from typing import Optional, Set, Tuple
import importlib.metadata
import logging
import asyncio
import time
import os
import io
import json
import hashlib

import apsw
import aiohttp
from aiohttp_middlewares import cors_middleware
from aiohttp import web
import jwt

import cbrrr

from . import static_config
from . import database
from . import auth_oauth
from . import atproto_sync
from . import atproto_repo
from . import util
from .appview_proxy import service_proxy
from .auth_bearer import authenticated
from .app_util import *

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


@web.middleware
async def atproto_service_proxy_middleware(request: web.Request, handler):
	# https://atproto.com/specs/xrpc#service-proxying
	atproto_proxy = request.headers.get("atproto-proxy")
	if atproto_proxy:
		return await service_proxy(request, atproto_proxy)

	# else, normal response
	res: web.Response = await handler(request)

	# inject security headers (this should really be a separate middleware, but here works too)
	res.headers.setdefault("X-Frame-Options", "DENY") # prevent clickajcking
	res.headers.setdefault("X-Content-Type-Options","nosniff") # prevent XSS (almost vestigial at this point, I think)
	res.headers.setdefault("Content-Security-Policy", "default-src 'none'; sandbox") # prevent everything
	# NB: HSTS and other TLS-related headers not set, set them in nginx or wherever you terminate TLS

	return res


@routes.get("/")
async def hello(request: web.Request):
	version = importlib.metadata.version("millipds")
	msg = f"""
                          ,dPYb, ,dPYb,                           8I
                          IP'`Yb IP'`Yb                           8I
                     gg   I8  8I I8  8I  gg                       8I
                     ""   I8  8' I8  8'  ""                       8I
  ,ggg,,ggg,,ggg,    gg   I8 dP  I8 dP   gg   gg,gggg,      ,gggg,8I     ,gg,
 ,8" "8P" "8P" "8,   88   I8dP   I8dP    88   I8P"  "Yb    dP"  "Y8I   ,8'8,
 I8   8I   8I   8I   88   I8P    I8P     88   I8'    ,8i  i8'    ,8I  ,8'  Yb
,dP   8I   8I   Yb,_,88,_,d8b,_ ,d8b,_ _,88,_,I8 _  ,d8' ,d8,   ,d8b,,8'_   8)
8P'   8I   8I   `Y88P""Y88P'"Y888P'"Y888P""Y8PI8 YY88888PP"Y8888P"`Y8P' "YY8P8P
                                              I8
                                              I8
                                              I8
                                              I8
                                              I8
                                              I8


Hello! This is an ATProto PDS instance, running millipds v{version}

https://github.com/DavidBuchanan314/millipds
"""

	return web.Response(text=msg)


@routes.get("/.well-known/did.json") # serve this server's did:web document (nb: reference PDS impl doesn't do this, hard to know the right thing to do)
async def well_known_did_web(request: web.Request):
	cfg = get_db(request).config
	return web.json_response({
		"@context": [
			"https://www.w3.org/ns/did/v1",
		],
		"id": cfg["pds_did"],
		"service": [{ # is this the right thing to do?
			"id": "#atproto_pds",
			"type": "AtprotoPersonalDataServer",
			"serviceEndpoint": cfg["pds_pfx"]
		}]
	})



@routes.get("/robots.txt")
async def robots_txt(request: web.Request):
	return web.Response(text="""\
# this is an atproto pds. please crawl it.

User-Agent: *
Allow: /
""")


# browsers love to request this unprompted, so here's an answer for them
@routes.get("/favicon.ico")
async def health(request: web.Request):
	return web.Response(
		text='''
			<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
				<text x="50%" y="0.95em" font-size="90" text-anchor="middle">🌐</text>
			</svg>
		''',
		content_type="image/svg+xml",
		headers={"Cache-Control": "max-age=864000"}
	)


# not a spec'd endpoint, but the reference impl has this too
@routes.get("/xrpc/_health")
async def health(request: web.Request):
	version = importlib.metadata.version("millipds")
	return web.json_response({
		"version": f"millipds v{version}"
	})

# we should not be implementing bsky-specific logic here!
# (ideally, a PDS should not be aware of app-specific logic)
@routes.get("/xrpc/app.bsky.actor.getPreferences")
async def actor_get_preferences(request: web.Request):
	return web.json_response({"preferences": []})  # dummy response

@routes.post("/xrpc/app.bsky.actor.putPreferences")
async def actor_put_preferences(request: web.Request):
	# TODO: actually implement this
	return web.Response()

@routes.get("/xrpc/com.atproto.identity.resolveHandle")
async def identity_resolve_handle(request: web.Request):
	# TODO: forward to appview(?) if we can't answer?
	handle = request.query.get("handle")
	if handle is None:
		return web.HTTPBadRequest(text="missing or invalid handle")
	did = get_db(request).did_by_handle(handle)
	if not did:
		return web.HTTPNotFound(text="no user by that handle exists on this PDS")
	return web.json_response({"did": did})





@routes.get("/xrpc/com.atproto.server.describeServer")
async def server_describe_server(request: web.Request):
	return web.json_response(
		{
			"did": get_db(request).config["pds_did"],
			"availableUserDomains": [],
		}
	)


# TODO: ratelimit this!!!
@routes.post("/xrpc/com.atproto.server.createSession")
async def server_create_session(request: web.Request):
	# extract the args
	try:
		req_json: dict = await request.json()
	except json.JSONDecodeError:
		return web.HTTPBadRequest(text="expected JSON")

	identifier = req_json.get("identifier")
	password = req_json.get("password")
	if not (isinstance(identifier, str) and isinstance(password, str)):
		return web.HTTPBadRequest(text="invalid identifier or password")

	# do authentication
	db = get_db(request)
	try:
		did, handle = db.verify_account_login(
			did_or_handle=identifier, password=password
		)
	except KeyError:
		raise web.HTTPUnauthorized(text="user not found")
	except ValueError:
		raise web.HTTPUnauthorized(text="incorrect identifier or password")

	# prepare access tokens
	unix_seconds_now = int(time.time())
	access_jwt = jwt.encode(
		{
			"scope": "com.atproto.access",
			"aud": db.config["pds_did"],
			"sub": did,
			"iat": unix_seconds_now,
			"exp": unix_seconds_now + 60 * 60 * 24,  # 24h
		},
		db.config["jwt_access_secret"],
		"HS256",
	)

	refresh_jwt = jwt.encode(
		{
			"scope": "com.atproto.refresh",
			"aud": db.config["pds_did"],
			"sub": did,
			"iat": unix_seconds_now,
			"exp": unix_seconds_now + 60 * 60 * 24 * 90,  # 90 days!
		},
		db.config["jwt_access_secret"],
		"HS256",
	)

	return web.json_response(
		{
			"did": did,
			"handle": handle,
			"accessJwt": access_jwt,
			"refreshJwt": refresh_jwt,
		}
	)





@routes.post("/xrpc/com.atproto.identity.updateHandle")
@authenticated
async def identity_update_handle(request: web.Request):
	req_json: dict = await request.json()
	handle = req_json.get("handle")
	if handle is None:
		return web.HTTPBadRequest(text="missing or invalid handle")
	# TODO: actually validate it, and update the db!!!
	# (I'm writing this half-baked version just so I can send firehose #identity events)
	with get_db(request).new_con() as con:
		# TODO: refactor to avoid duplicated logic between here and apply_writes
		firehose_seq = con.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM firehose").fetchone()[0]
		firehose_bytes = cbrrr.encode_dag_cbor({
			"t": "#identity",
			"op": 1
		}) + cbrrr.encode_dag_cbor({
			"seq": firehose_seq,
			"did": request["authed_did"],
			"time": util.iso_string_now(),
			"handle": handle
		})
		con.execute(
			"INSERT INTO firehose (seq, timestamp, msg) VALUES (?, ?, ?)",
			(firehose_seq, 0, firehose_bytes) # TODO: put sensible timestamp here...
		)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))

	# temp hack: #account events shouldn't really be generated here
	with get_db(request).new_con() as con:
		# TODO: refactor to avoid duplicated logic between here and apply_writes
		firehose_seq = con.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM firehose").fetchone()[0]
		firehose_bytes = cbrrr.encode_dag_cbor({
			"t": "#account",
			"op": 1
		}) + cbrrr.encode_dag_cbor({
			"seq": firehose_seq,
			"did": request["authed_did"],
			"time": util.iso_string_now(),
			"active": True
		})
		con.execute(
			"INSERT INTO firehose (seq, timestamp, msg) VALUES (?, ?, ?)",
			(firehose_seq, 0, firehose_bytes) # TODO: put sensible timestamp here...
		)
	await atproto_repo.firehose_broadcast(request, (firehose_seq, firehose_bytes))

	return web.Response()


@routes.get("/xrpc/com.atproto.server.getSession")
@authenticated
async def server_get_session(request: web.Request):
	return web.json_response(
		{
			"handle": get_db(request).handle_by_did(request["authed_did"]),
			"did": request["authed_did"],
			"email": "tfw_no@email.invalid",  # this and below are just here for testing lol
			"emailConfirmed": True,
			#"didDoc": {}, # iiuc this is only used for entryway usecase?
		}
	)


def construct_app(routes, db: database.Database) -> web.Application:
	cors = cors_middleware( # TODO: review and reduce scope - and maybe just /xrpc/*?
		allow_all=True,
		expose_headers=["*"],
		allow_headers=["*"],
		allow_methods=["*"],
		allow_credentials=True,
		max_age=2_000_000_000
	)

	app = web.Application(middlewares=[cors, atproto_service_proxy_middleware])
	app["MILLIPDS_DB"] = db
	app["MILLIPDS_AIOHTTP_CLIENT"] = (
		aiohttp.ClientSession()
	)  # should this be dependency-injected?
	app["MILLIPDS_FIREHOSE_QUEUES"] = set()
	app["MILLIPDS_FIREHOSE_QUEUES_LOCK"] = asyncio.Lock()
	app.add_routes(routes)
	app.add_routes(auth_oauth.routes)
	app.add_routes(atproto_sync.routes)
	app.add_routes(atproto_repo.routes)

	# list of routes to proxy to the appview - hopefully not needed in the future (we'll derive the list from lexicons? and/or maybe service-proxying would be used?) https://github.com/bluesky-social/atproto/discussions/2350#discussioncomment-11193778
	app.add_routes(
		[
			# fmt off
			# web.get ("/xrpc/app.bsky.actor.getPreferences", static_appview_proxy),
			# web.post("/xrpc/app.bsky.actor.putPreferences", static_appview_proxy),
			web.get("/xrpc/app.bsky.actor.getProfile", service_proxy),
			web.get("/xrpc/app.bsky.actor.getProfiles", service_proxy),
			web.get("/xrpc/app.bsky.actor.getSuggestions", service_proxy),
			web.get("/xrpc/app.bsky.actor.searchActorsTypeahead", service_proxy),
			web.get("/xrpc/app.bsky.labeler.getServices", service_proxy),
			web.get("/xrpc/app.bsky.notification.listNotifications", service_proxy),
			web.get("/xrpc/app.bsky.notification.getUnreadCount", service_proxy),
			web.post("/xrpc/app.bsky.notification.updateSeen", service_proxy),
			web.get("/xrpc/app.bsky.graph.getList", service_proxy),
			web.get("/xrpc/app.bsky.graph.getLists", service_proxy),
			web.get("/xrpc/app.bsky.graph.getFollows", service_proxy),
			web.get("/xrpc/app.bsky.graph.getFollowers", service_proxy),
			web.get("/xrpc/app.bsky.graph.getStarterPack", service_proxy),
			web.get("/xrpc/app.bsky.graph.getSuggestedFollowsByActor", service_proxy),
			web.get("/xrpc/app.bsky.graph.getActorStarterPacks", service_proxy),
			web.post("/xrpc/app.bsky.graph.muteActor", service_proxy),
			web.post("/xrpc/app.bsky.graph.unmuteActor", service_proxy),
			web.get("/xrpc/app.bsky.feed.getTimeline", service_proxy),
			web.get("/xrpc/app.bsky.feed.getAuthorFeed", service_proxy),
			web.get("/xrpc/app.bsky.feed.getActorFeeds", service_proxy),
			web.get("/xrpc/app.bsky.feed.getFeed", service_proxy),
			web.get("/xrpc/app.bsky.feed.getListFeed", service_proxy),
			web.get("/xrpc/app.bsky.feed.getFeedGenerator", service_proxy),
			web.get("/xrpc/app.bsky.feed.getFeedGenerators", service_proxy),
			web.get("/xrpc/app.bsky.feed.getPostThread", service_proxy),
			web.get("/xrpc/app.bsky.feed.getPosts", service_proxy),
			web.get("/xrpc/app.bsky.feed.getLikes", service_proxy),
			web.get("/xrpc/app.bsky.feed.getActorLikes", service_proxy),
			web.get("/xrpc/app.bsky.unspecced.getPopularFeedGenerators", service_proxy),
			#web.get("/xrpc/chat.bsky.convo.listConvos", static_appview_proxy)
			# fmt on
		]
	)

	return app


async def run(db: database.Database, sock_path: Optional[str], host: str, port: int):
	"""
	This gets invoked via millipds.__main__.py
	"""

	app = construct_app(routes, db)
	runner = web.AppRunner(app, access_log_format=static_config.HTTP_LOG_FMT)
	await runner.setup()

	if sock_path is None:
		logger.info(f"listening on http://{host}:{port}")
		site = web.TCPSite(runner, host=host, port=port)
	else:
		logger.info(f"listening on {sock_path}")
		site = web.UnixSite(runner, path=sock_path)

	await site.start()

	if sock_path:
		# give group access to the socket (so that nginx can access it via a shared group)
		# see https://github.com/aio-libs/aiohttp/issues/4155#issuecomment-693979753
		import grp

		try:
			sock_gid = grp.getgrnam(static_config.GROUPNAME).gr_gid
			os.chown(sock_path, os.geteuid(), sock_gid)
		except KeyError:
			logger.warning(
				f"Failed to set socket group - group {static_config.GROUPNAME!r} not found."
			)
		except PermissionError:
			logger.warning(
				f"Failed to set socket group - are you a member of the {static_config.GROUPNAME!r} group?"
			)

		os.chmod(sock_path, 0o770)

	while True:
		await asyncio.sleep(3600)  # sleep forever

#!/usr/bin/env python3
"""
rtsp_webrtc_server.py

Low-latency RTSP→WebRTC server for NVIDIA Jetson using GStreamer:

 • Hardware-accelerated decode (nvv4l2decoder) and encode (nvv4l2h264enc)
 • drop-on-latency + small jitter buffer to prevent frame backlog
 • HTTP endpoints for SDP offer, ICE candidates, stop, plus SSE heartbeat
 • Detailed logging at every stage to help trace and debug
"""

import asyncio
import logging
import os
import yaml
from aiohttp import web
from gi.repository import Gst

# ─── Initialize GStreamer ─────────────────────────────────────────────────────
Gst.init(None)

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rtsp-webrtc")

# ─── Global state ──────────────────────────────────────────────────────────────
pipelines = set()   # track active pipelines so we can clean them up

# ─── Load configuration ───────────────────────────────────────────────────────
ROOT = os.path.dirname(__file__)
with open(os.path.join(ROOT, "config.yaml"), "r") as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg["camera"]["webrtc"]["stun_server"]

# ─── HTTP /stream/offer ────────────────────────────────────────────────────────
async def handle_offer(request):
    """
    POST /stream/offer
    { sdp: "<client SDP>", type: "offer" }
    → builds and PLAYs a new GStreamer pipeline, logs the SDP answer.
    """
    params = await request.json()
    client_sdp  = params.get("sdp")
    client_type = params.get("type")
    logger.info("▷ Received SDP %s", client_type)

    try:
        pipeline = build_pipeline(client_sdp, client_type)
        pipelines.add(pipeline)
        pipeline.set_state(Gst.State.PLAYING)
        logger.info("✅ Pipeline PLAYING")
        return web.json_response({"success": True})
    except Exception as e:
        logger.exception("✖ Error in handle_offer")
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ─── HTTP /stream/ice-candidate ───────────────────────────────────────────────
async def handle_ice(request):
    """
    POST /stream/ice-candidate
    { candidate: "...", sdpMLineIndex: 0, sdpMid: "0" }
    → forwards to every active webrtcbin.
    """
    data = await request.json()
    cand = data.get("candidate")
    mid  = data.get("sdpMid")
    idx  = data.get("sdpMLineIndex")
    logger.info("▷ ICE candidate (mid=%s, idx=%s): %s", mid, idx, cand)

    for pipe in list(pipelines):
        webrtc = pipe.get_by_name("webrtcbin")
        try:
            webrtc.emit("add-ice-candidate", idx, cand)
            logger.debug("  • forwarded ICE to pipeline")
        except Exception:
            logger.exception("  ✖ failed to forward ICE")
    return web.json_response({"success": True})

# ─── HTTP /stream/stop ────────────────────────────────────────────────────────
async def handle_stop(request):
    """
    POST /stream/stop
    → tears down all pipelines.
    """
    logger.info("▷ Stopping all pipelines")
    for pipe in list(pipelines):
        pipe.set_state(Gst.State.NULL)
        pipelines.discard(pipe)
    return web.json_response({"success": True})

# ─── HTTP /stream/heartbeat (SSE) ─────────────────────────────────────────────
async def sse_heartbeat(request):
    """
    GET /stream/heartbeat
    Emits a ": ping" comment every 10s to keep EventSource alive.
    """
    logger.info("▷ SSE heartbeat client connected")
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
    await resp.prepare(request)

    try:
        while True:
            await resp.write(b": ping\n\n")
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("▷ SSE heartbeat client disconnected")
    return resp

# ─── Build GStreamer pipeline ─────────────────────────────────────────────────
def build_pipeline(client_sdp, client_type):
    """
    Create a hardware-accelerated pipeline:

    rtspsrc (drop-on-latency) → rtph264depay → h264parse → nvv4l2decoder
      → nvvidconv → nvv4l2h264enc → rtph264pay → webrtcbin

    Hooks up:
     • on-negotiation-needed → create-offer → logs SDP answer
     • on-ice-candidate         → logs ICE candidates
    """
    logger.info("Building pipeline for %s → webrtc", RTSP_URL)

    # ── Create pipeline + elements
    pipe   = Gst.Pipeline.new(None)
    src    = Gst.ElementFactory.make("rtspsrc",      "rtspsrc")
    depay  = Gst.ElementFactory.make("rtph264depay", "depay")
    parse  = Gst.ElementFactory.make("h264parse",    "parse")
    decode = Gst.ElementFactory.make("nvv4l2decoder","decode")
    conv   = Gst.ElementFactory.make("nvvidconv",    "convert")
    enc    = Gst.ElementFactory.make("nvv4l2h264enc","encode")
    pay    = Gst.ElementFactory.make("rtph264pay",   "pay")
    webrtc = Gst.ElementFactory.make("webrtcbin",    "webrtcbin")

    for e in (src,depay,parse,decode,conv,enc,pay,webrtc):
        if not e:
            raise RuntimeError(f"Failed to create {e.get_name() if e else 'element'}")

    # ── Configure RTSP source
    src.set_property("location",          RTSP_URL)
    src.set_property("latency",           100)          # 100 ms jitter buffer
    src.set_property("drop-on-latency",   True)         # drop old frames, avoid backlog

    # ── Configure encoder + STUN
    enc.set_property("bitrate", 2000000)                # 2 Mbps target
    webrtc.set_property("stun-server", STUN_SERVER)

    # ── Assemble pipeline
    pipe.add(src,depay,parse,decode,conv,enc,pay,webrtc)
    depay.link(parse)
    parse.link(decode)
    decode.link(conv)
    conv.link(enc)
    enc.link(pay)

    # ── Link pay → webrtcbin via explicit request_pad
    tmpl    = webrtc.get_pad_template("sink_%u")
    sinkpad = webrtc.request_pad(tmpl, None, None)
    srcpad  = pay.get_static_pad("src")
    srcpad.link(sinkpad)
    logger.debug("  • Linked pay.src → webrtcbin.sink")

    # ── Dynamic link: rtspsrc → depay
    def on_pad_added(src, pad):
        logger.debug("  • rtspsrc dynamic pad added, linking to depay")
        pad.link(depay.get_static_pad("sink"))
    src.connect("pad-added", on_pad_added)

    # ── WebRTC callbacks
    def on_negotiation_needed(element):
        logger.info("⟳ webrtcbin on-negotiation-needed")
        promise = Gst.Promise.new_with_change_func(on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def on_offer_created(promise, element, _):
        promise.wait()
        answer = element.get_property("local-description")
        logger.info("✓ Created SDP answer:\n%s", answer.sdp)
        # — if you had a POST /stream/answer endpoint, you'd send it here
        # e.g.: requests.post("…/stream/answer", json={"sdp": answer.sdp, "type": answer.type})

    def on_ice_candidate(element, mlineindex, candidate):
        logger.info("⟳ ICE→ %s", candidate)

    webrtc.connect("on-negotiation-needed", on_negotiation_needed)
    webrtc.connect("on-ice-candidate",       on_ice_candidate)

    return pipe

# ─── HTTP Server ──────────────────────────────────────────────────────────────
app = web.Application()
app.router.add_post("/stream/offer",        handle_offer)
app.router.add_post("/stream/ice-candidate",handle_ice)
app.router.add_post("/stream/stop",         handle_stop)
app.router.add_get ("/stream/heartbeat",    sse_heartbeat)

if __name__ == "__main__":
    # Cleanly handle Ctrl+C
    web.run_app(app, host="0.0.0.0", port=8000)

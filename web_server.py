#!/usr/bin/env python3
import os
import json
import threading
import logging
import yaml
from aiohttp import web

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GObject

# ──────────────────────────────────────────────────────────────
# Configure logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("webrtc-server")

# ──────────────────────────────────────────────────────────────
# Load configuration
# ──────────────────────────────────────────────────────────────
THIS_DIR  = os.path.dirname(__file__)
CONF_PATH = os.path.join(THIS_DIR, "config.yaml")
with open(CONF_PATH) as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg.get("webrtc", {}).get("stun_server", "stun://stun.l.google.com:19302")
CSV_LOG     = cfg.get("log_behavior", {}).get("csv_path", "")

logger.info(f"RTSP URL       : {RTSP_URL}")
logger.info(f"STUN server    : {STUN_SERVER}")
logger.info(f"CSV log (stub) : {CSV_LOG}")

# ──────────────────────────────────────────────────────────────
# Initialize GStreamer
# ──────────────────────────────────────────────────────────────
Gst.init(None)
GObject.threads_init()

pipeline   = None
webrtcbin  = None
pending_ice = []
sdp_answer  = None

# ──────────────────────────────────────────────────────────────
# GStreamer callbacks
# ──────────────────────────────────────────────────────────────
def on_ice_candidate(elem, mlineindex, candidate):
    """Called by webrtcbin when it has a new local ICE candidate."""
    logger.debug(f"New ICE candidate from GStreamer (mline={mlineindex}): {candidate}")
    pending_ice.append({
        "sdpMid": None,
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })

def _on_rtsp_pad(rtspsrc, pad):
    """Link rtspsrc → decodebin dynamically."""
    logger.debug(f"RTSP src pad added: {pad.get_name()}")
    decode = pipeline.get_by_name("decodebin")
    sink_pad = decode.get_static_pad("sink")
    if not sink_pad:
        logger.error("No sink pad on decodebin!")
        return
    pad.link(sink_pad)
    decode.connect("pad-added", _on_decode_pad)

def _on_decode_pad(decodebin, pad):
    """Link decodebin → videoconvert dynamically."""
    logger.debug(f"Decodebin pad added: {pad.get_name()}")
    convert = pipeline.get_by_name("convert")
    sink_pad = convert.get_static_pad("sink")
    if not sink_pad:
        logger.error("No sink pad on videoconvert!")
        return
    pad.link(sink_pad)

# ──────────────────────────────────────────────────────────────
# Build & link the GStreamer pipeline
# ──────────────────────────────────────────────────────────────
def build_pipeline():
    global pipeline, webrtcbin

    logger.info("Building GStreamer pipeline...")
    pipeline = Gst.Pipeline.new("webrtc-pipeline")

    # Elements
    src     = Gst.ElementFactory.make("rtspsrc",     "rtspsrc")
    decode  = Gst.ElementFactory.make("decodebin",   "decodebin")
    convert = Gst.ElementFactory.make("videoconvert","convert")
    encoder = Gst.ElementFactory.make("x264enc",     "encoder")
    pay     = Gst.ElementFactory.make("rtph264pay",  "payloader")
    webrtcbin = Gst.ElementFactory.make("webrtcbin", "webrtcbin")

    if not all((pipeline, src, decode, convert, encoder, pay, webrtcbin)):
        logger.critical("Failed to create one or more GStreamer elements")
        raise RuntimeError("Missing GStreamer elements")

    # Configure elements
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    encoder.set_property("tune", "zerolatency")
    pay.set_property("config-interval", 1)
    webrtcbin.set_property("stun-server", STUN_SERVER)

    # Add to pipeline
    for el in (src, decode, convert, encoder, pay, webrtcbin):
        pipeline.add(el)

    # Static linking
    convert.link(encoder)
    encoder.link(pay)

    # Dynamic pad handlers
    src.connect("pad-added",     _on_rtsp_pad)
    decode.connect("pad-added",  _on_decode_pad)

    # Link payloader → webrtcbin sink pad
    pad_src = pay.get_static_pad("src")
    if not pad_src:
        logger.critical("rtph264pay has no src pad")
        raise RuntimeError("Payloader pad missing")

    # Request a sink pad on webrtcbin
    pad_sink = webrtcbin.request_pad("sink_%u")
    if not pad_sink:
        logger.critical("Failed to request sink pad from webrtcbin")
        raise RuntimeError("webrtcbin sink pad request failed")

    ret = pad_src.link(pad_sink)
    if ret != Gst.PadLinkReturn.OK:
        logger.critical(f"Failed to link payloader to webrtcbin ({ret})")
        raise RuntimeError("Pad link failure")

    # Connect ICE callback
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    logger.info("GStreamer pipeline built successfully")
    return pipeline

def gst_main():
    """Thread target to start & run the GStreamer pipeline."""
    try:
        p = build_pipeline()
        p.set_state(Gst.State.PLAYING)
        logger.info("Pipeline set to PLAYING")
        GObject.MainLoop().run()
    except Exception as e:
        logger.exception(f"Error in GStreamer main loop: {e}")

# ──────────────────────────────────────────────────────────────
# HTTP Signaling Handlers (matching your old endpoints)
# ──────────────────────────────────────────────────────────────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def handle_offer(request):
    global sdp_answer, pending_ice
    payload = await request.json()
    offer = payload.get("offer") or payload
    sdp   = offer.get("sdp")
    logger.info("Received SDP offer from client")

    # Parse and set remote description
    msg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp.encode(), msg)
    webrtcbin.emit("set-remote-description",
                   GstWebRTC.WebRTCSessionDescription.new(
                       GstWebRTC.WebRTCSDPType.OFFER, msg))
    logger.debug("Remote description set on webrtcbin")

    # Create answer
    def on_answer(promise, _, __):
        global sdp_answer
        reply = promise.get_reply()
        answer = reply["answer"]
        sdp_answer = answer.sdp.as_text()
        logger.debug("Generated SDP answer")

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtcbin.emit("create-answer", None, promise)

    # Wait for the answer
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    response = {
        "sdp":  sdp_answer,
        "type": "answer",
        "ice":  pending_ice.copy()
    }
    logger.info("Sending SDP answer + ICE candidates to client")
    pending_ice.clear()
    sdp_answer = None
    return web.json_response(response)

@routes.post("/stream/ice-candidate")
async def handle_ice(request):
    data = await request.json()
    cand = data.get("candidate", data)
    logger.info(f"Received ICE candidate from client: {cand}")
    webrtcbin.emit("add-ice-candidate",
                   cand["sdpMLineIndex"],
                   cand["candidate"])
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def handle_stop(request):
    logger.info("Received stop request; setting pipeline to NULL")
    pipeline.set_state(Gst.State.NULL)
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def handle_status(request):
    state = pipeline.get_state(0)[1]
    name  = Gst.Element.state_get_name(state)
    logger.debug(f"Status request: pipeline is {name}")
    return web.json_response({"status": name})

@routes.get("/log")
async def handle_log(request):
    logger.debug("Log endpoint hit; returning empty list")
    return web.json_response([])

# ──────────────────────────────────────────────────────────────
# Application entrypoint
# ──────────────────────────────────────────────────────────────
def main():
    # 1) Start GStreamer in background
    threading.Thread(target=gst_main, daemon=True).start()
    # 2) Start HTTP server
    app = web.Application()
    app.add_routes(routes)
    logger.info("Starting HTTP server on port 8000")
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

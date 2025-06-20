#!/usr/bin/env python3
import os
import json
import threading
import logging
import yaml
from aiohttp import web

import gi
gi.require_version('Gst',      '1.0')
gi.require_version('GstWebRTC','1.0')
gi.require_version('GstSdp',   '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GObject

# ─────────── Configure Logging ───────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rtsp-ws")

# ─────────── Load config.yaml ───────────
THIS_DIR  = os.path.dirname(__file__)
CONF_PATH = os.path.join(THIS_DIR, "config.yaml")
with open(CONF_PATH, "r") as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg.get("webrtc", {}).get(
    "stun_server", "stun://stun.l.google.com:19302"
)
CSV_LOG     = cfg.get("log_behavior", {}).get("csv_path", "")

logger.info(f"RTSP URL    : {RTSP_URL}")
logger.info(f"STUN Server : {STUN_SERVER}")
logger.info(f"CSV Log     : {CSV_LOG} (stub)")

# ─────────── GStreamer Init ───────────
Gst.init(None)
GObject.threads_init()

pipeline    = None
webrtcbin   = None
pending_ice = []
sdp_answer  = None

# ─────────── GStreamer Callbacks ───────────
def on_ice_candidate(elem, mlineindex, candidate):
    logger.debug(f"[GStreamer] ICE candidate mline={mlineindex}: {candidate}")
    pending_ice.append({
        "sdpMid": None,
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })

def _on_rtsp_pad(src, pad):
    logger.debug(f"[GStreamer] rtspsrc pad-added: {pad.get_name()}")
    decode = pipeline.get_by_name("decodebin")
    sink   = decode.get_static_pad("sink")
    if not sink:
        logger.error("decodebin missing sink pad")
        return
    pad.link(sink)
    decode.connect("pad-added", _on_decode_pad)

def _on_decode_pad(decode, pad):
    logger.debug(f"[GStreamer] decodebin pad-added: {pad.get_name()}")
    convert = pipeline.get_by_name("convert")
    sink    = convert.get_static_pad("sink")
    if not sink:
        logger.error("videoconvert missing sink pad")
        return
    pad.link(sink)

# ────────── Build & Link Pipeline ──────────
def build_pipeline():
    global pipeline, webrtcbin

    logger.info("Building GStreamer pipeline…")
    pipeline = Gst.Pipeline.new("webrtc-pipeline")

    # Create elements
    src       = Gst.ElementFactory.make("rtspsrc",     "rtspsrc")
    decode    = Gst.ElementFactory.make("decodebin",   "decodebin")
    convert   = Gst.ElementFactory.make("videoconvert","convert")
    encoder   = Gst.ElementFactory.make("x264enc",     "encoder")
    payloader = Gst.ElementFactory.make("rtph264pay",  "payloader")
    webrtcbin = Gst.ElementFactory.make("webrtcbin",   "webrtcbin")

    if not all((pipeline, src, decode, convert, encoder, payloader, webrtcbin)):
        logger.critical("Failed to create GStreamer elements")
        raise RuntimeError("GStreamer element creation failure")

    # Configure
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    encoder.set_property("tune", "zerolatency")
    payloader.set_property("config-interval", 1)
    webrtcbin.set_property("stun-server", STUN_SERVER)

    # Add elements
    for el in (src, decode, convert, encoder, payloader, webrtcbin):
        pipeline.add(el)

    # Static link: convert → encoder → payloader
    convert.link(encoder)
    encoder.link(payloader)

    # Dynamic RTSP decode
    src.connect("pad-added",     _on_rtsp_pad)
    decode.connect("pad-added",  _on_decode_pad)

    # Request sink pad on webrtcbin
    pad_template = webrtcbin.get_pad_template("sink_%u")
    if not pad_template:
        logger.critical("webrtcbin missing pad-template 'sink_%u'")
        raise RuntimeError("Missing webrtcbin pad-template")

    pad_sink = webrtcbin.request_pad(pad_template, None)
    if not pad_sink:
        logger.critical("Failed to request sink pad from webrtcbin")
        raise RuntimeError("webrtcbin.request_pad() failed")

    # Link payloader → webrtcbin
    pad_src = payloader.get_static_pad("src")
    if not pad_src:
        logger.critical("payloader missing src pad")
        raise RuntimeError("Missing payloader src pad")

    ret = pad_src.link(pad_sink)
    if ret != Gst.PadLinkReturn.OK:
        logger.critical(f"Failed to link payloader → webrtcbin ({ret})")
        raise RuntimeError("Pad link failure")

    # ICE callback
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    logger.info("Pipeline built and linked successfully")
    return pipeline

def gst_main():
    """Run GStreamer mainloop in background."""
    try:
        p = build_pipeline()
        p.set_state(Gst.State.PLAYING)
        logger.info("Pipeline state → PLAYING")
        GObject.MainLoop().run()
    except Exception:
        logger.exception("Error in GStreamer main loop")

# ─────────── HTTP Signaling Endpoints ───────────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def handle_offer(request):
    global sdp_answer, pending_ice
    payload = await request.json()
    offer   = payload.get("offer", payload)
    sdp     = offer["sdp"]
    logger.info("Received /stream/offer")

    # Create empty SDPMessage
    # GstSdp.SDPMessage.new() returns a tuple (msg, result)
    msg, _ = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp.encode(), msg)
    webrtcbin.emit("set-remote-description",
                   GstWebRTC.WebRTCSessionDescription.new(
                       GstWebRTC.WebRTCSDPType.OFFER, msg))
    logger.debug("Remote SDP set on webrtcbin")

    # Create answer
    def on_answer(promise, _, __):
        global sdp_answer
        reply      = promise.get_reply()
        answer_obj = reply["answer"]
        sdp_answer = answer_obj.sdp.as_text()
        logger.debug("SDP answer generated")

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtcbin.emit("create-answer", None, promise)

    # Wait for answer
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    resp = {
        "sdp":  sdp_answer,
        "type": "answer",
        "ice":  pending_ice.copy()
    }
    pending_ice.clear()
    sdp_answer = None

    logger.info("Sending SDP answer + ICE to client")
    return web.json_response(resp)

@routes.post("/stream/ice-candidate")
async def handle_ice(request):
    data = await request.json()
    cand = data.get("candidate", data)
    logger.info(f"Received /stream/ice-candidate: {cand}")
    webrtcbin.emit("add-ice-candidate",
                   cand["sdpMLineIndex"],
                   cand["candidate"])
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def handle_stop(request):
    logger.info("Received /stream/stop → setting pipeline to NULL")
    pipeline.set_state(Gst.State.NULL)
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def handle_status(request):
    state = pipeline.get_state(0)[1]
    name  = Gst.Element.state_get_name(state)
    logger.info(f"Received /stream/status → {name}")
    return web.json_response({"status": name})

@routes.get("/log")
async def handle_log(request):
    logger.debug("Received /log → returning empty list")
    return web.json_response([])

# ─────────── Application Entry ───────────
def main():
    threading.Thread(target=gst_main, daemon=True).start()
    app = web.Application()
    app.add_routes(routes)
    logger.info("Starting HTTP server on 0.0.0.0:8000")
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

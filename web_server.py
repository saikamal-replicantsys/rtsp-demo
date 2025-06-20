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

# ─────────── Logging ───────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webrtc-server")

# ─────────── Load config.yaml ───────────
THIS_DIR  = os.path.dirname(__file__)
CONF_PATH = os.path.join(THIS_DIR, "config.yaml")
with open(CONF_PATH) as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg.get("webrtc", {}).get("stun_server",
                                         "stun://stun.l.google.com:19302")
CSV_LOG     = cfg.get("log_behavior", {}).get("csv_path", "")

logger.info(f"RTSP URL    : {RTSP_URL}")
logger.info(f"STUN server : {STUN_SERVER}")
logger.info(f"CSV log     : {CSV_LOG}")

# ───────────── GStreamer Init ─────────────
Gst.init(None)
GObject.threads_init()

pipeline    = None
webrtcbin   = None
pending_ice = []
sdp_answer  = None  # global for answer handshake

# ───────── ICE callback ─────────
def on_ice_candidate(elem, mlineindex, candidate):
    logger.debug(f"[GStreamer] New ICE candidate: mline={mlineindex}, cand={candidate}")
    pending_ice.append({
        "sdpMid": None,
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })

# ───────── Dynamic RTSP decode pads ─────────
def _on_rtsp_pad(src, pad):
    logger.debug(f"[GStreamer] rtspsrc pad-added: {pad.get_name()}")
    decode = pipeline.get_by_name("decodebin")
    sink = decode.get_static_pad("sink")
    if not sink:
        logger.error("decodebin has no sink pad!")
        return
    pad.link(sink)
    decode.connect("pad-added", _on_decode_pad)

def _on_decode_pad(decode, pad):
    logger.debug(f"[GStreamer] decodebin pad-added: {pad.get_name()}")
    convert = pipeline.get_by_name("convert")
    sink = convert.get_static_pad("sink")
    if not sink:
        logger.error("videoconvert has no sink pad!")
        return
    pad.link(sink)

# ───────── Build pipeline ─────────
def build_pipeline():
    global pipeline, webrtcbin

    logger.info("Building GStreamer pipeline…")
    pipeline = Gst.Pipeline.new("webrtc-pipeline")

    # Elements
    src      = Gst.ElementFactory.make("rtspsrc",     "rtspsrc")
    decode   = Gst.ElementFactory.make("decodebin",   "decodebin")
    convert  = Gst.ElementFactory.make("videoconvert","convert")
    encoder  = Gst.ElementFactory.make("x264enc",     "encoder")
    pay      = Gst.ElementFactory.make("rtph264pay",  "payloader")
    webrtcbin= Gst.ElementFactory.make("webrtcbin",   "webrtcbin")

    # Check
    if not all((pipeline, src, decode, convert, encoder, pay, webrtcbin)):
        logger.critical("Failed to create all GStreamer elements")
        raise RuntimeError("GStreamer element creation failure")

    # Configure
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    encoder.set_property("tune", "zerolatency")
    pay.set_property("config-interval", 1)
    webrtcbin.set_property("stun-server", STUN_SERVER)

    # Add to pipeline
    for el in (src, decode, convert, encoder, pay, webrtcbin):
        pipeline.add(el)

    # Static link: convert → encoder → payloader
    convert.link(encoder)
    encoder.link(pay)

    # Dynamic: RTSP decode
    src.connect("pad-added",     _on_rtsp_pad)
    decode.connect("pad-added",  _on_decode_pad)

    # Now request webrtcbin sink pad template
    pad_template = webrtcbin.get_pad_template("sink_%u")
    if not pad_template:
        logger.critical("webrtcbin has no pad template 'sink_%u'")
        raise RuntimeError("Missing webrtcbin pad template")

    pad_sink = webrtcbin.request_pad(pad_template, None)
    if not pad_sink:
        logger.critical("Failed to request webrtcbin sink pad")
        raise RuntimeError("webrtcbin.request_pad() failed")

    # Link payloader → webrtcbin sink
    pad_src = pay.get_static_pad("src")
    if not pad_src:
        logger.critical("payloader has no src pad")
        raise RuntimeError("Missing payloader src pad")

    ret = pad_src.link(pad_sink)
    if ret != Gst.PadLinkReturn.OK:
        logger.critical(f"pad link failed: {ret}")
        raise RuntimeError(f"pad_src.link(pad_sink) returned {ret}")

    # ICE callback
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    logger.info("Pipeline built successfully")
    return pipeline

def gst_main():
    """Thread runner for GStreamer's main loop."""
    try:
        p = build_pipeline()
        p.set_state(Gst.State.PLAYING)
        logger.info("Pipeline state: PLAYING")
        GObject.MainLoop().run()
    except Exception:
        logger.exception("Error in GStreamer loop")

# ───────── HTTP Signaling routes ─────────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def handle_offer(req):
    global sdp_answer, pending_ice
    data = await req.json()
    # Support { offer: { sdp, type }} or { sdp, type }
    offer = data.get("offer", data)
    sdp   = offer["sdp"]
    logger.info("Received /stream/offer")

    # Set remote description
    msg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp.encode(), msg)
    webrtcbin.emit("set-remote-description",
                   GstWebRTC.WebRTCSessionDescription.new(
                     GstWebRTC.WebRTCSDPType.OFFER, msg))
    logger.debug("Remote SDP set")

    # Create answer
    def on_answer(promise, _, __):
        global sdp_answer
        reply = promise.get_reply()
        ans   = reply["answer"]
        sdp_answer = ans.sdp.as_text()
        logger.debug("SDP answer ready")

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtcbin.emit("create-answer", None, promise)

    # Wait for answer
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    response = {"sdp": sdp_answer,
                "type": "answer",
                "ice":  pending_ice.copy()}
    pending_ice.clear()
    sdp_answer = None
    logger.info("Replying to /stream/offer with answer+ICE")
    return web.json_response(response)

@routes.post("/stream/ice-candidate")
async def handle_ice(req):
    data = await req.json()
    cand = data.get("candidate", data)
    logger.info(f"Received /stream/ice-candidate: {cand}")
    webrtcbin.emit("add-ice-candidate",
                   cand["sdpMLineIndex"],
                   cand["candidate"])
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def handle_stop(req):
    logger.info("Received /stream/stop")
    pipeline.set_state(Gst.State.NULL)
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def handle_status(req):
    state = pipeline.get_state(0)[1]
    name  = Gst.Element.state_get_name(state)
    logger.info(f"Received /stream/status → {name}")
    return web.json_response({"status": name})

@routes.get("/log")
async def handle_log(req):
    logger.info("Received /log → returning stub []")
    return web.json_response([])

# ─────────── Application main ───────────
def main():
    # 1) Start GStreamer thread
    threading.Thread(target=gst_main, daemon=True).start()
    # 2) Start HTTP server
    app = web.Application()
    app.add_routes(routes)
    logger.info("Starting HTTP server on 0.0.0.0:8000")
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

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

# ─── Logging ─────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rtsp-ws")

# ─── Load configuration ───────────────────────────────
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

# ─── GStreamer Initialization ────────────────────────
Gst.init(None)
GObject.threads_init()

pipeline    = None
webrtcbin   = None
pending_ice = []
sdp_answer  = None

# ─── GStreamer Callbacks ─────────────────────────────
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

# ─── Build & Link Pipeline ───────────────────────────
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
        logger.critical("Failed to create one or more GStreamer elements")
        raise RuntimeError("Element creation failure")

    # Configure elements
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    encoder.set_property("tune", "zerolatency")
    payloader.set_property("config-interval", 1)
    webrtcbin.set_property("stun-server", STUN_SERVER)

    # Add elements to pipeline
    for el in (src, decode, convert, encoder, payloader, webrtcbin):
        pipeline.add(el)

    # Static links
    convert.link(encoder)
    encoder.link(payloader)

    # Dynamic RTSP decode
    src.connect("pad-added",     _on_rtsp_pad)
    decode.connect("pad-added",  _on_decode_pad)

    # Discover and log pad templates for webrtcbin
    templates = webrtcbin.get_pad_template_list()
    logger.debug("webrtcbin pad templates:")
    for tmpl in templates:
        logger.debug("  • %s", tmpl.get_name_template())

    # Pick the sink template named "sink_%u" if available, else first containing "sink"
    pad_template = None
    for tmpl in templates:
        name = tmpl.get_name_template()
        if name == "sink_%u":
            pad_template = tmpl
            break
    if not pad_template:
        for tmpl in templates:
            if "sink" in tmpl.get_name_template():
                pad_template = tmpl
                logger.warning("Using fallback pad template %s", tmpl.get_name_template())
                break

    if not pad_template:
        logger.critical("No suitable sink pad template found on webrtcbin")
        raise RuntimeError("Missing webrtcbin sink pad template")

    # Request a sink pad
    pad_sink = webrtcbin.request_pad(pad_template, None)
    if not pad_sink:
        logger.critical("webrtcbin.request_pad() failed for template %s",
                        pad_template.get_name_template())
        raise RuntimeError("Pad request failure")

    # Link payloader → webrtcbin sink
    pad_src = payloader.get_static_pad("src")
    if not pad_src:
        logger.critical("payloader missing src pad")
        raise RuntimeError("Missing payloader src pad")

    ret = pad_src.link(pad_sink)
    if ret != Gst.PadLinkReturn.OK:
        logger.critical("Failed to link payloader → webrtcbin (%s)", ret)
        raise RuntimeError("Pad link failure")

    # ICE callback
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    logger.info("GStreamer pipeline built and linked")
    return pipeline

def gst_main():
    """Run GStreamer's mainloop in a background thread."""
    try:
        p = build_pipeline()
        p.set_state(Gst.State.PLAYING)
        logger.info("Pipeline state → PLAYING")
        GObject.MainLoop().run()
    except Exception:
        logger.exception("Error in GStreamer main loop")

# ─── HTTP Signaling Endpoints ───────────────────────────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def handle_offer(request):
    global sdp_answer, pending_ice
    data  = await request.json()
    offer = data.get("offer", data)
    sdp   = offer["sdp"]
    logger.info("Received /stream/offer")

    # Parse and set remote SDP
    msg, _ = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp.encode(), msg)
    webrtcbin.emit("set-remote-description",
                   GstWebRTC.WebRTCSessionDescription.new(
                       GstWebRTC.WebRTCSDPType.OFFER, msg))
    logger.debug("Remote description set")

    # Create answer
    def on_answer(promise, _, __):
        global sdp_answer
        reply      = promise.get_reply()
        answer_obj = reply["answer"]
        sdp_answer = answer_obj.sdp.as_text()
        logger.debug("SDP answer ready")

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtcbin.emit("create-answer", None, promise)

    # Wait for answer
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    resp = {"sdp": sdp_answer, "type": "answer", "ice": pending_ice.copy()}
    pending_ice.clear()
    sdp_answer = None

    logger.info("Sending SDP answer + ICE candidates")
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
    logger.info("Received /stream/stop → NULL pipeline")
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
    logger.debug("Received /log → returning []")
    return web.json_response([])

# ─── App Entrypoint ───────────────────────────────────────
def main():
    threading.Thread(target=gst_main, daemon=True).start()
    app = web.Application()
    app.add_routes(routes)
    logger.info("Starting HTTP server on 0.0.0.0:8000")
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

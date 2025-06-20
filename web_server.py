# web_server.py

import os
import threading
import asyncio
from aiohttp import web
import yaml
import gi

# ──────────────────────────────────────────────────────────────
# Initialize GStreamer
# ──────────────────────────────────────────────────────────────
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')

from gi.repository import Gst, GstWebRTC, GstSdp, GObject

Gst.init(None)
GObject.threads_init()

# ──────────────────────────────────────────────────────────────
# Load config (to get your RTSP URL and STUN server)
# ──────────────────────────────────────────────────────────────
THIS_DIR    = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(THIS_DIR, "config.yaml")

with open(CONFIG_PATH) as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg.get("webrtc", {}).get("stun_server", "stun://stun.l.google.com:19302")

# ──────────────────────────────────────────────────────────────
# Build the GStreamer webrtcbin pipeline
# ──────────────────────────────────────────────────────────────

# Single global pipeline & webrtcbin element
pipeline = None
webrtc   = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
webrtc.set_property("stun-server", STUN_SERVER)

# Store ICE candidates from GStreamer to send back later
pending_ice = []

def on_ice_candidate(_elem, mlineindex, candidate):
    """Callback from webrtcbin when it generates an ICE candidate."""
    pending_ice.append({
        "sdpMid": None,
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })

webrtc.connect("on-ice-candidate", on_ice_candidate)

def build_pipeline():
    """Constructs: rtspsrc → decode → queue → webrtcbin sink
                     webrtcbin src → rtph264pay → webrtcbin."""

    global pipeline, webrtc
    pipeline = Gst.Pipeline.new("webrtc-pipeline")

    # RTSP source & decoder
    src = Gst.ElementFactory.make("rtspsrc", "rtspsrc")
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    src.connect("pad-added", on_rtsp_pad)

    decode = Gst.ElementFactory.make("decodebin", "decodebin")
    pipeline.add(src)
    pipeline.add(decode)

    # Encoder path: raw → convert → x264enc → rtph264pay
    convert = Gst.ElementFactory.make("videoconvert", "convert")
    encoder = Gst.ElementFactory.make("x264enc", "encoder")
    encoder.set_property("tune", "zerolatency")
    pay = Gst.ElementFactory.make("rtph264pay", "pay")
    pay.set_property("config-interval", 1)

    # Add webrtcbin
    pipeline.add(convert)
    pipeline.add(encoder)
    pipeline.add(pay)
    pipeline.add(webrtc)

    # Link decoder → convert → encoder → pay → webrtc src pad
    convert.link(encoder)
    encoder.link(pay)
    pay_src = pay.get_static_pad("src")
    webrtc_sink = webrtc.get_request_pad("sink_%u")
    pay_src.link(webrtc_sink)

    return pipeline

def on_rtsp_pad(src, pad):
    """When rtspsrc gives us a pad, link it to decodebin."""
    decode = pipeline.get_by_name("decodebin")
    pad.link(decode.get_static_pad("sink"))

    # When decodebin outputs raw video, link into videoconvert
    decode.connect("pad-added", on_decode_pad)

def on_decode_pad(dec, pad):
    """Link the decodebin’s raw video output into videoconvert."""
    convert = pipeline.get_by_name("convert")
    pad.link(convert.get_static_pad("sink"))

# ──────────────────────────────────────────────────────────────
# HTTP Signaling Handlers (same routes your Node.js expects)
# ──────────────────────────────────────────────────────────────
routes = web.RouteTableDef()
sdp_answer = None

@routes.post("/stream/offer")
async def offer(request):
    """
    Receives JSON { sdp, type } from your Node.js backend,
    sets remote descr on webrtcbin, creates an answer, and returns it.
    """
    global sdp_answer, pending_ice

    data = await request.json()
    offer_sdp = data["sdp"]

    # Parse SDP and set remote on webrtcbin
    sdp = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(offer_sdp.encode(), sdp)
    webrtc.emit("set-remote-description", GstWebRTC.WebRTCSessionDescription.new(
        GstWebRTC.WebRTCSDPType.OFFER, sdp))

    # Create answer
    def on_answer(promise, _idx, _):
        nonlocal sdp_answer
        reply = promise.get_reply()
        answer = reply["answer"]
        sdp_answer = answer.sdp.as_text()

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtc.emit("create-answer", None, promise)

    # Spin the GObject loop until we have an answer
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    # Return answer + any ICE candidates that appeared immediately
    body = {"sdp": sdp_answer, "type": "answer", "ice": pending_ice.copy()}
    # Clear once sent
    pending_ice.clear()
    return web.json_response(body)

@routes.post("/stream/ice-candidate")
async def ice_candidate(request):
    """
    Receives { candidate: { candidate, sdpMid, sdpMLineIndex } }
    from Node.js and injects into webrtcbin.
    """
    data = await request.json()
    c = data["candidate"]
    webrtc.emit("add-ice-candidate", c["sdpMLineIndex"], c["candidate"])
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def stop(request):
    """Stop the pipeline."""
    pipeline.set_state(Gst.State.NULL)
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def status(request):
    """Return PLAYING vs. NULL etc."""
    state = pipeline.get_state(0)[1]
    return web.json_response({"status": Gst.Element.state_get_name(state)})

# ──────────────────────────────────────────────────────────────
# Boot it all up
# ──────────────────────────────────────────────────────────────
def start_gst_loop():
    build_pipeline().set_state(Gst.State.PLAYING)
    # Run the GLib MainLoop to drive GStreamer
    GObject.MainLoop().run()

def main():
    # 1) Start GStreamer in its own thread
    threading.Thread(target=start_gst_loop, daemon=True).start()

    # 2) Start aiohttp server on port 8000
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

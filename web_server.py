import os
import json
import threading
import yaml
from aiohttp import web

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GObject

# ───── Load Config ─────
THIS_DIR  = os.path.dirname(__file__)
conf_path = os.path.join(THIS_DIR, "config.yaml")
with open(conf_path) as f:
    cfg = yaml.safe_load(f)

RTSP_URL    = cfg["camera"]["rtsp_url"]
STUN_SERVER = cfg.get("webrtc", {}).get("stun_server", "stun://stun.l.google.com:19302")
CSV_LOG     = os.path.join(THIS_DIR, cfg.get("log_behavior", {}).get("csv_path", ""))

# ───── GStreamer Init ─────
Gst.init(None)
GObject.threads_init()

pipeline    = None
webrtcbin   = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
webrtcbin.set_property("stun-server", STUN_SERVER)

pending_ice = []
sdp_answer  = None  # now treated as a global

def on_ice_candidate(elem, mlineindex, candidate):
    pending_ice.append({
        "sdpMid": None,
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })
webrtcbin.connect("on-ice-candidate", on_ice_candidate)

# ───── Build Pipeline ─────
def _on_rtsp_pad(src, pad):
    decode = pipeline.get_by_name("decodebin")
    pad.link(decode.get_static_pad("sink"))
    decode.connect("pad-added", _on_decode_pad)

def _on_decode_pad(decode, pad):
    convert = pipeline.get_by_name("convert")
    pad.link(convert.get_static_pad("sink"))

def build_pipeline():
    global pipeline
    pipeline = Gst.Pipeline.new("webrtc-pipeline")

    src = Gst.ElementFactory.make("rtspsrc", "rtspsrc")
    src.set_property("location", RTSP_URL)
    src.set_property("latency", 200)
    src.connect("pad-added", _on_rtsp_pad)
    pipeline.add(src)

    decode  = Gst.ElementFactory.make("decodebin", "decodebin")
    convert = Gst.ElementFactory.make("videoconvert", "convert")
    encoder = Gst.ElementFactory.make("x264enc", "encoder")
    encoder.set_property("tune", "zerolatency")
    pay     = Gst.ElementFactory.make("rtph264pay", "pay")
    pay.set_property("config-interval", 1)

    for el in (decode, convert, encoder, pay, webrtcbin):
        pipeline.add(el)

    convert.link(encoder)
    encoder.link(pay)
    pad_src = pay.get_static_pad("src")
    pad_sink = webrtcbin.get_request_pad("sink_%u")
    pad_src.link(pad_sink)

    return pipeline

def gst_main():
    build_pipeline().set_state(Gst.State.PLAYING)
    GObject.MainLoop().run()

# ───── HTTP Signaling ─────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def handle_offer(request):
    global sdp_answer, pending_ice
    data = await request.json()
    # support both { offer: { sdp, type } } and { sdp, type }
    offer_sdp = (data.get("offer") or data).get("sdp")

    # set remote
    msg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(offer_sdp.encode(), msg)
    webrtcbin.emit("set-remote-description",
                   GstWebRTC.WebRTCSessionDescription.new(
                       GstWebRTC.WebRTCSDPType.OFFER, msg))

    # prepare answer
    def on_answer(promise, _, __):
        global sdp_answer
        reply = promise.get_reply()
        ans   = reply["answer"]
        sdp_answer = ans.sdp.as_text()

    promise = Gst.Promise.new_with_change_func(on_answer, None, None)
    webrtcbin.emit("create-answer", None, promise)

    # wait for answer to be set
    while sdp_answer is None:
        GObject.MainContext.default().iteration(True)

    body = {
        "sdp":  sdp_answer,
        "type": "answer",
        "ice":  pending_ice.copy()
    }
    pending_ice.clear()
    sdp_answer = None
    return web.json_response(body)

@routes.post("/stream/ice-candidate")
async def handle_ice(request):
    data = await request.json()
    c    = data.get("candidate", data)
    webrtcbin.emit("add-ice-candidate", c["sdpMLineIndex"], c["candidate"])
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def handle_stop(request):
    pipeline.set_state(Gst.State.NULL)
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def handle_status(request):
    state = pipeline.get_state(0)[1]
    return web.json_response({"status": Gst.Element.state_get_name(state)})

@routes.get("/log")
async def handle_log(request):
    # stub returning empty list
    return web.json_response([])

# ───── App Runner ─────
def main():
    threading.Thread(target=gst_main, daemon=True).start()
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

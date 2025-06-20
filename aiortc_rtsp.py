#!/usr/bin/env python3
import os
import json
import logging
import asyncio
import yaml
import cv2
import numpy as np

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame

# ─────────── Logging ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rtsp-aiortc")

# ─────────── Load config.yaml ───────────
THIS_DIR  = os.path.dirname(__file__)
with open(os.path.join(THIS_DIR, "config.yaml")) as f:
    cfg = yaml.safe_load(f)

RTSP_URL = cfg["camera"]["rtsp_url"]
CSV_LOG  = cfg.get("log_behavior", {}).get("csv_path", "")

logger.info(f"RTSP URL    : {RTSP_URL}")
logger.info(f"CSV log     : {CSV_LOG} (stub)")

# ─────────── PeerConnections ───────────
pcs = set()

# ─────────── Video Track ───────────
class RTSPTrack(VideoStreamTrack):
    """A VideoStreamTrack that pulls frames from an RTSP URL."""
    def __init__(self, rtsp_url):
        super().__init__()  # don't forget this!
        self.cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open RTSP stream: {rtsp_url}")
        logger.info("Opened RTSP stream for WebRTC track")

    async def recv(self):
        # Throttle to camera FPS by waiting for next timestamp
        pts, time_base = await self.next_timestamp()

        ret, frame = self.cap.read()
        if not ret:
            logger.warning("RTSP frame read failed, reopening capture")
            self.cap.release()
            await asyncio.sleep(1)
            self.cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
            ret, frame = self.cap.read()
            if not ret:
                # send a black frame instead
                frame = np.zeros((480, 640, 3), dtype=np.uint8)

        # Convert BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

# ─────────── HTTP / WebRTC Signaling ───────────
routes = web.RouteTableDef()

@routes.post("/stream/offer")
async def offer(request):
    """
    Client sends { sdp, type }.
    We create a new RTCPeerConnection, add our RTSPTrack, then return { sdp, type } answer.
    """
    params = await request.json()
    offer_sdp  = params["sdp"]
    offer_type = params["type"]
    logger.info("Received /stream/offer")

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        logger.info("PeerConnection state → %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.discard(pc)

    # Add our RTSP-based video track
    pc.addTrack(RTSPTrack(RTSP_URL))

    # Apply remote offer
    await pc.setRemoteDescription(RTCSessionDescription(offer_sdp, offer_type))

    # Create & send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logger.info("Sending SDP answer")
    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )

@routes.post("/stream/ice-candidate")
async def ice_candidate(request):
    """
    Client sends { candidate: { candidate, sdpMid, sdpMLineIndex } }.
    We add it to the single active PeerConnection.
    """
    data = await request.json()
    cand = data["candidate"]
    logger.info("Received /stream/ice-candidate: %s", cand)

    # Pick any existing PC
    pc = next(iter(pcs), None)
    if pc:
        await pc.addIceCandidate(candidate_from_sdp(cand["candidate"]))
    return web.json_response({"success": True})

@routes.post("/stream/stop")
async def stop(request):
    """
    Tear down all PeerConnections.
    """
    logger.info("Received /stream/stop")
    for pc in list(pcs):
        await pc.close()
    pcs.clear()
    return web.json_response({"success": True})

@routes.get("/stream/status")
async def status(request):
    """
    Return the connectionState of the first PeerConnection,
    or "closed" if none.
    """
    pc = next(iter(pcs), None)
    state = pc.connectionState if pc else "closed"
    logger.info("Received /stream/status → %s", state)
    return web.json_response({"status": state})

@routes.get("/log")
async def get_log(request):
    """
    Stub for /log — return empty list for now.
    """
    logger.debug("Received /log → returning []")
    return web.json_response([])

# ─────────── Application Entrypoint ───────────
def main():
    app = web.Application()
    app.add_routes(routes)
    logger.info("Starting HTTP server on 0.0.0.0:8000")
    web.run_app(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()

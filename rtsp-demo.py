#!/usr/bin/env python3
import gi
import cv2
import threading

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')

from gi.repository import Gst, GstRtspServer, GObject, GLib

# Initialize GStreamer
Gst.init(None)

class CameraFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self):
        super(CameraFactory, self).__init__()
        self.cap = cv2.VideoCapture(0)  # change index if needed
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.frame_count = 0

    def do_create_element(self, url):
        # Build a GStreamer pipeline string: appsrc → videoconvert → x264enc → rtph264pay
        # appsrc caps: match OpenCV's BGR frames at 1280×720 @30fps
        pipeline_str = (
            "appsrc name=mysrc is-live=true block=true format=GST_FORMAT_TIME "
            "caps=video/x-raw,format=BGR,width=1280,height=720,framerate=30/1 "
            "! videoconvert ! video/x-raw,format=I420 "
            "! x264enc tune=zerolatency bitrate=800 speed-preset=superfast "
            "! rtph264pay name=pay0 pt=96"
        )
        return Gst.parse_launch(pipeline_str)

    def do_configure(self, rtsp_media):
        # Called when a client connects; get appsrc and start pushing frames
        appsrc = rtsp_media.get_element().get_child_by_name("mysrc")

        def push_frames():
            while True:
                ret, frame = self.cap.read()
                if not ret:
                    continue

                # Convert the OpenCV BGR frame to raw bytes
                data = frame.tobytes()
                buf = Gst.Buffer.new_allocate(None, len(data), None)
                buf.fill(0, data)

                # Each frame is 1/30 second long
                duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 30)
                buf.duration = duration
                timestamp = self.frame_count * duration
                buf.pts = buf.dts = int(timestamp)
                buf.offset = self.frame_count
                self.frame_count += 1

                # Push the buffer into appsrc
                retval = appsrc.emit("push-buffer", buf)
                if retval != Gst.FlowReturn.OK:
                    break

        threading.Thread(target=push_frames, daemon=True).start()

class RTSPServer:
    def __init__(self):
        self.server = GstRtspServer.RTSPServer()
        self.factory = CameraFactory()
        self.factory.set_shared(True)
        mounts = self.server.get_mount_points()
        mounts.add_factory("/test", self.factory)
        self.server.attach(None)

if __name__ == '__main__':
    loop = GLib.MainLoop()
    s = RTSPServer()
    print("RTSP stream available at rtsp://0.0.0.0:8554/test")
    loop.run()

# A virtual camera on the player, fed from a laptop — for testing camera-driven
# pieces without a webcam at the box (or with a recorded clip / another machine's
# camera).
#
#   /dev/video10  "td-deploy virtual camera"  (v4l2loopback)
#
# Anything on the box that opens a camera sees it as an ordinary V4L2 webcam: a
# project's own capture code (e.g. OpenCV index 10), or a Video Device In TOP.
#
# The feeder (td-camfeed.service) listens on 127.0.0.1:8090 — loopback only, never
# on the network — for a stream of concatenated JPEG frames and decodes it into
# the device. The laptop side, app/toxc_camstream.py, captures a camera / file /
# URL, encodes MJPEG and sends it through an SSH tunnel (the deploy key), so no
# new port is exposed on either machine. When a sender disconnects, the feeder
# restarts and waits for the next one; the device keeps its last format.
{ config, lib, pkgs, ... }:
let
  videoNr = 10;
  port = 8090;
in
{
  boot.extraModulePackages = [ config.boot.kernelPackages.v4l2loopback ];
  boot.kernelModules = [ "v4l2loopback" ];
  # exclusive_caps=1: the device only advertises CAPTURE once a producer is
  # attached, which is what Chrome/OpenCV-style consumers expect of a webcam.
  boot.extraModprobeConfig = ''
    options v4l2loopback video_nr=${toString videoNr} card_label="td-deploy virtual camera" exclusive_caps=1
  '';

  systemd.services.td-camfeed = {
    description = "Feed a streamed MJPEG camera into /dev/video${toString videoNr}";
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-modules-load.service" ];
    serviceConfig = {
      # One sender per run: ffmpeg exits when the stream ends; wait for the next.
      Restart = "always";
      RestartSec = 1;
      DynamicUser = true;
      SupplementaryGroups = [ "video" ];
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.ffmpeg-headless}/bin/ffmpeg -hide_banner -loglevel warning"
        "-fflags nobuffer -flags low_delay"
        "-f mjpeg -i tcp://127.0.0.1:${toString port}?listen=1"
        "-vf format=yuyv422 -f v4l2 /dev/video${toString videoNr}"
      ];
    };
  };
}

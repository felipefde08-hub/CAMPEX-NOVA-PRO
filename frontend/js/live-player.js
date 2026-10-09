// Low-latency live view of a CAMPEX Node camera: go2rtc's fMP4 stream (MSE)
// relayed by the Node at /api/cameras/<id>/live.
import { VideoRTC } from "../vendor/video-rtc.js";

const TAG = "campex-live-video";

if (!customElements.get(TAG)) {
  customElements.define(TAG, VideoRTC);
}

export function createLivePlayer(url) {
  const player = document.createElement(TAG);
  // MSE, or MP4 for old iPhones. No WebRTC: go2rtc only listens on the
  // Node's computer, and WebRTC would contact public STUN servers.
  player.mode = "mse,mp4";
  player.media = "video";
  player.className = "live-media live-player";
  player.src = url;
  return player;
}

// 播放器：把“当前实际渠道版本”的 media_id + etag 随进度回调。
// 重剪后旧音频回调（旧 id/etag）会被服务端以 409 拒绝，且不会污染新时间线。
(function () {
  var box = document.querySelector('.player[data-media-id]');
  if (!box) return;
  var audio = box.querySelector('audio');
  var status = document.getElementById('cb-status');
  var mediaId = box.getAttribute('data-media-id');
  var etag = box.getAttribute('data-etag');
  var epMatch = location.pathname.match(/\/e\/(\d+|[^/]+)$/);

  function send() {
    var m = location.pathname.match(/\/e\/[^/]+$/);
    fetch('/api/current-episode?slug=' + encodeURIComponent(location.pathname.split('/').pop()))
      .then(function (r) { return r.json(); })
      .then(function (info) {
        return fetch('/api/episodes/' + info.episode_id + '/progress', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ media_id: mediaId, position_ms: Math.round(audio.currentTime * 1000), etag: etag })
        }).then(function (r) { return { r: r }; });
      })
      .then(function (o) {
        if (o.r.status === 409) {
          status.textContent = '⚠ 旧版本回调已被服务端拒绝，请刷新获取最新渠道版本。';
          status.style.color = '#b91c1c';
        }
      })
      .catch(function () {});
  }
  audio.addEventListener('pause', send);
  audio.addEventListener('timeupdate', function () {
    // 节流：每 ~5s 一次
    if (!audio._last || audio.currentTime - audio._last > 5) { audio._last = audio.currentTime; send(); }
  });

  document.querySelectorAll('.jump').forEach(function (b) {
    b.addEventListener('click', function () {
      if (audio) { audio.currentTime = (+b.getAttribute('data-t')) / 1000; audio.play(); }
    });
  });
})();

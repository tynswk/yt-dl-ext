const QUALITY = {
  mp4: [["best", "最高画質"], ["1080", "1080p まで"], ["720", "720p まで"], ["480", "480p まで"]],
  mp3: [["0", "最高音質 (VBR ~245k)"], ["5", "標準 (~130k)"], ["9", "軽量 (~65k)"]],
  wav: [["best", "16bit PCM"]],
};

const $ = (id) => document.getElementById(id);
let url = null;
let fmt = "mp4";

const fail = (msg) => {
  $("error").textContent = msg;
  $("error").hidden = false;
};

function fillQuality() {
  $("quality").innerHTML = "";
  for (const [v, label] of QUALITY[fmt]) $("quality").add(new Option(label, v));
  $("quality").disabled = QUALITY[fmt].length < 2;
}

function render(job) {
  if (!job) return;
  $("progress").hidden = false;
  $("fill").style.width = `${job.percent || 0}%`;
  $("fill").classList.toggle("done", job.state === "done");
  $("go").disabled = job.state === "running" || !url;

  if (job.state === "done") {
    $("ptext").textContent = "完了";
  } else if (job.state === "error") {
    $("progress").hidden = true;
    fail(job.error || "失敗しました");
  } else if (job.status) {
    $("ptext").textContent = job.status;
  } else {
    const bits = [`${(job.percent || 0).toFixed(1)}%`];
    if (job.speed) bits.push(job.speed);
    if (job.eta) bits.push(`残り ${job.eta}`);
    $("ptext").textContent = job.percent ? bits.join("  ") : "準備中…";
  }
}

$("format").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-v]");
  if (!btn) return;
  fmt = btn.dataset.v;
  for (const b of $("format").children) b.classList.toggle("on", b === btn);
  fillQuality();
  chrome.storage.local.set({ fmt });
});

$("reveal").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reveal" }).catch(() => {});
});

async function saveOutDir() {
  const dir = $("outdir").value.trim();
  if (!dir || dir === $("outdir").dataset.current) return;
  const res = await chrome.runtime.sendMessage({ type: "setOutDir", dir });
  if (res?.ok) {
    $("error").hidden = true;
    $("outdir").value = res.outDir;
    $("outdir").dataset.current = res.outDir;
    $("outdir").classList.add("saved");
    setTimeout(() => $("outdir").classList.remove("saved"), 1200);
  } else {
    fail(res?.error || "保存先を変更できませんでした");
    $("outdir").value = $("outdir").dataset.current || "";
  }
}

$("outdir").addEventListener("change", saveOutDir);
$("outdir").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("outdir").blur();
});

$("go").addEventListener("click", () => {
  $("error").hidden = true;
  $("go").disabled = true;
  $("progress").hidden = false;
  $("fill").classList.remove("done");
  $("fill").style.width = "0%";
  $("ptext").textContent = "準備中…";
  chrome.runtime.sendMessage({
    type: "download", url, format: fmt, quality: $("quality").value,
  }).catch((e) => fail(e.message));
});

// ダウンロード中にポップアップを開き直しても進捗が続いて見える
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "state") render(msg.job);
});

(async function init() {
  const { fmt: saved } = await chrome.storage.local.get("fmt");
  if (saved && QUALITY[saved]) {
    fmt = saved;
    for (const b of $("format").children) b.classList.toggle("on", b.dataset.v === fmt);
  }
  fillQuality();

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const ok = tab?.url && /^https:\/\/((www|m|music)\.)?youtube\.com\/watch|^https:\/\/youtu\.be\//.test(tab.url);
  if (ok) {
    url = tab.url.split("&list=")[0];
    $("title").textContent = (tab.title || "").replace(/ - YouTube$/, "");
  } else {
    $("title").textContent = "YouTube の動画ページを開いてください";
    $("go").disabled = true;
  }

  const { job, health } = await chrome.runtime.sendMessage({ type: "getState" });

  if (health?.error || health?.ytdlp === false) {
    $("dot").className = "dot ng";
    $("status").textContent = "ホストに接続できない";
    $("go").disabled = true;
    fail(health.error || "yt-dlp が見つかりません: pip install -U yt-dlp");
    return;
  }

  $("dot").className = "dot ok";
  $("status").textContent = health.ffmpeg ? "準備完了" : "準備完了（ffmpeg なし）";
  $("outdir").value = health.outDir || "";
  $("outdir").dataset.current = health.outDir || "";
  render(job);

  if (ok && job?.state !== "running") {
    const info = await chrome.runtime.sendMessage({ type: "info", url });
    if (info?.title) $("title").textContent = info.title;
    if (!info?.error) {
      $("meta").textContent = [info.uploader, info.duration].filter(Boolean).join("  ·  ");
    }
  }
})();

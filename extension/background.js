// ネイティブホストとの接続を一手に持つ。ポップアップは開いたり閉じたりするので、
// ダウンロード中の接続をポップアップに持たせるとウィンドウを閉じた瞬間に中断してしまう。

const HOST = "com.ytdlext.host";
const IDLE_MS = 4000; // 用が済んでからホストを落とすまでの猶予

let port = null;
let idleTimer = null;
let health = null;
const pendingInfo = new Map(); // reqId -> resolve

// ポップアップが開き直したときに復元するための現在の状態
let job = null; // {jobId, state, percent, speed, eta, file, error, format}

function connect() {
  if (port) return port;
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(onMessage);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError?.message;
    port = null;
    health = null;
    for (const resolve of pendingInfo.values()) resolve({ error: err || "接続が切れました" });
    pendingInfo.clear();
    if (job && job.state === "running") {
      job = { ...job, state: "error", error: err || "ホストとの接続が切れました" };
      broadcast();
    }
  });
  return port;
}

// 実行中の仕事が無ければホストを終了させる（= 常駐させない）
function scheduleIdleShutdown() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (port && !pendingInfo.size && job?.state !== "running") {
      port.disconnect();
      port = null;
      health = null;
    }
  }, IDLE_MS);
}

function broadcast() {
  chrome.runtime.sendMessage({ type: "state", job }).catch(() => {});
}

function onMessage(msg) {
  switch (msg.type) {
    case "health":
      health = msg;
      break;
    case "outDir":
    case "info": {
      const resolve = pendingInfo.get(msg.reqId);
      if (resolve) {
        pendingInfo.delete(msg.reqId);
        resolve(msg);
      }
      scheduleIdleShutdown();
      break;
    }
    case "started":
      job = { ...job, jobId: msg.jobId, state: "running", percent: 0 };
      broadcast();
      break;
    case "progress":
      if (job?.jobId !== msg.jobId) break;
      job = { ...job, percent: msg.percent, speed: msg.speed, eta: msg.eta, status: null };
      broadcast();
      break;
    case "status": // yt-dlp の更新中など、進捗の代わりに出す一言
      if (job?.jobId !== msg.jobId) break;
      job = { ...job, status: msg.text };
      broadcast();
      break;
    case "done":
      job = { ...job, state: "done", percent: 100, file: msg.file };
      broadcast();
      notifyDone();
      scheduleIdleShutdown();
      break;
    case "error":
      job = { ...job, state: "error", error: msg.error };
      broadcast();
      scheduleIdleShutdown();
      break;
  }
}

// ポップアップを閉じていても完了がわかるようにバッジで出す
function notifyDone() {
  chrome.action.setBadgeText({ text: "✓" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 20000);
}

function send(msg) {
  connect().postMessage(msg);
}

function askHealth() {
  return new Promise((resolve) => {
    if (health) return resolve(health);
    try {
      send({ type: "health" });
    } catch (e) {
      return resolve({ error: e.message });
    }
    const started = Date.now();
    const tick = setInterval(() => {
      if (health) {
        clearInterval(tick);
        resolve(health);
      // Python の起動を待つ。初回は数秒かかることがあるので短く切らない。
      } else if (!port || Date.now() - started > 15000) {
        clearInterval(tick);
        resolve({ error: "ネイティブホストが応答しません" });
      }
    }, 60);
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "getState") {
    askHealth().then((h) => sendResponse({ job, health: h }));
    scheduleIdleShutdown();
    return true;
  }

  // info と setOutDir はどちらも reqId で応答を待つ同じ仕組みに乗せる
  if (msg.type === "info" || msg.type === "setOutDir") {
    const reqId = crypto.randomUUID();
    new Promise((resolve) => {
      pendingInfo.set(reqId, resolve);
      send({ ...msg, reqId });
    }).then(sendResponse);
    return true;
  }

  if (msg.type === "download") {
    clearTimeout(idleTimer);
    chrome.action.setBadgeText({ text: "" });
    job = { jobId: null, state: "running", percent: 0, format: msg.format };
    send({ type: "download", url: msg.url, format: msg.format, quality: msg.quality });
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === "reveal") {
    send({ type: "reveal", file: job?.file || null });
    scheduleIdleShutdown();
    sendResponse({ ok: true });
    return false;
  }
});

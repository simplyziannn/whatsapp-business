const $ = (id) => document.getElementById(id);

function appendLog(obj) {
  const line = JSON.stringify(obj);
  $("log").textContent = line + "\n" + $("log").textContent;
}

function setMetrics(r) {
  $("mCacheHit").textContent = String(r.cache_hit);
  $("mRetrieval").textContent = String(r.t_retrieval_ms);
  $("mTotal").textContent = String(r.t_total_ms);
  $("mContextLen").textContent = String(r.context_len);
}

async function callOnce() {
  const payload = {
    from_number: $("fromNumber").value.trim(),
    text: $("question").value.trim(),
    disable_cache: $("disableCache").checked
  };

  const res = await fetch("/debug/cache_test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  const data = await res.json();
  appendLog({ status: res.status, ...data });
  if (data.ok) setMetrics(data);
}

$("sendBtn").addEventListener("click", async () => {
  $("sendBtn").disabled = true;
  try { await callOnce(); }
  finally { $("sendBtn").disabled = false; }
});

$("sendTwiceBtn").addEventListener("click", async () => {
  $("sendTwiceBtn").disabled = true;
  try {
    await callOnce();
    await callOnce();
  } finally {
    $("sendTwiceBtn").disabled = false;
  }
});

$("clearLogBtn").addEventListener("click", () => {
  $("log").textContent = "";
  $("mCacheHit").textContent = "-";
  $("mRetrieval").textContent = "-";
  $("mTotal").textContent = "-";
  $("mContextLen").textContent = "-";
});

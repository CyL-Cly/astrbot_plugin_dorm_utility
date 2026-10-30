const out = document.getElementById("out");
const btn = document.getElementById("btn");

function bridge() {
  return window.AstrBotPluginView || window.AstrBotPluginPage || null;
}

function show(obj) {
  out.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}

btn.addEventListener("click", async () => {
  btn.disabled = true;
  show("发送中…");
  try {
    const b = bridge();
    if (!b || typeof b.apiPost !== "function") {
      throw new Error("未找到 AstrBot 插件视图 bridge，请从仪表盘插件详情打开本页。");
    }
    if (typeof b.ready === "function") {
      await Promise.race([b.ready(), new Promise((r) => setTimeout(r, 1500))]);
    }
    const result = await b.apiPost("send-test", {});
    show(result);
  } catch (err) {
    show(`失败：${err?.message || err}`);
  } finally {
    btn.disabled = false;
  }
});

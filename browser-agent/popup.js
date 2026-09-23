const PATH = "/web/3/tariff/cpa/profile?entryPoint=desktop_profile";

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(n / 100) + " ₽";
}

function findHolder(root) {
  const queue = [root];
  const seen = new Set();

  while (queue.length) {
    const value = queue.shift();
    if (!value || typeof value !== "object") continue;
    if (seen.has(value)) continue;
    seen.add(value);

    if (
      Object.prototype.hasOwnProperty.call(value, "advanceThreshold") ||
      Object.prototype.hasOwnProperty.call(value, "advanceBalance") ||
      Object.prototype.hasOwnProperty.call(value, "cpaAlert")
    ) {
      return value;
    }

    const children = Array.isArray(value) ? value : Object.values(value);
    for (const child of children) {
      if (child && typeof child === "object") queue.push(child);
    }
  }

  return null;
}

function alertCode(value) {
  if (!value) return null;
  if (typeof value === "string") return value;

  const queue = [value];
  const seen = new Set();

  while (queue.length) {
    const item = queue.shift();
    if (!item || typeof item !== "object") continue;
    if (seen.has(item)) continue;
    seen.add(item);

    for (const child of Object.values(item)) {
      if (typeof child === "string" && child.startsWith("CPA_")) return child;
      if (child && typeof child === "object") queue.push(child);
    }
  }
  return null;
}

function showError(text) {
  const el = document.getElementById("error");
  el.textContent = text;
  el.classList.remove("hidden");
}

function clearError() {
  const el = document.getElementById("error");
  el.textContent = "";
  el.classList.add("hidden");
}

async function fetchInsidePage(tabId) {
  const execution = chrome.scripting.executeScript({
    target: {tabId},
    world: "MAIN",
    func: async (path) => {
      try {
        const response = await fetch(path, {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          headers: {"Accept": "application/json, text/plain, */*"}
        });

        const text = await response.text();
        let payload = null;
        try {
          payload = JSON.parse(text);
        } catch (_) {}

        return {
          ok: response.ok,
          status: response.status,
          payload,
          text: text.slice(0, 1000)
        };
      } catch (error) {
        return {
          ok: false,
          status: 0,
          payload: null,
          text: String(error?.stack || error?.message || error)
        };
      }
    },
    args: [PATH]
  });

  const result = await Promise.race([
    execution,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Запрос к вкладке Avito не завершился за 12 секунд")), 12000)
    )
  ]);

  return result?.[0]?.result;
}

async function runCheck() {
  clearError();

  const button = document.getElementById("check");
  button.disabled = true;
  button.textContent = "Проверяю…";

  try {
    const tabs = await chrome.tabs.query({active: true, currentWindow: true});
    const tab = tabs?.[0];

    if (!tab?.id) {
      throw new Error("Не удалось определить активную вкладку.");
    }

    if (!tab.url?.startsWith("https://www.avito.ru/")) {
      throw new Error("Сначала открой активную вкладку Avito в этом окне Edge.");
    }

    const reply = await fetchInsidePage(tab.id);

    if (!reply) {
      throw new Error("Edge не вернул результат из вкладки Avito.");
    }

    if (!reply.ok) {
      throw new Error(`Avito HTTP ${reply.status}: ${reply.text || "без ответа"}`);
    }

    const holder = findHolder(reply.payload);
    if (!holder) {
      throw new Error(
        "Avito ответил 200, но поля advanceBalance / advanceThreshold / cpaAlert не найдены."
      );
    }

    const balance = holder.advanceBalance ?? null;
    const threshold = holder.advanceThreshold ?? null;
    const cpaAlert = holder.cpaAlert ?? null;
    const code = alertCode(cpaAlert);

    const balanceNum = Number(balance);
    const thresholdNum = Number(threshold);
    const stoppedByBalance =
      Number.isFinite(balanceNum) &&
      Number.isFinite(thresholdNum) &&
      balanceNum < thresholdNum;

    const stopped =
      stoppedByBalance ||
      code === "CPA_NOT_ENOUGH_ADVANCE" ||
      code === "CPA_VIEWS_PAUSED";

    document.getElementById("balance").textContent = money(balance);
    document.getElementById("threshold").textContent = money(threshold);
    document.getElementById("state").textContent =
      stopped ? "Показы остановлены" : "Показы активны";
    document.getElementById("alert").textContent = code || "null";
    document.getElementById("checked").textContent =
      new Date().toLocaleString("ru-RU");

    await chrome.storage.local.set({
      lastAdvanceBalance: balance,
      lastAdvanceThreshold: threshold,
      lastCpaAlertCode: code,
      lastStopped: stopped,
      lastCheckAt: new Date().toISOString(),
      lastError: ""
    });
  } catch (error) {
    const text = String(error?.message || error);
    showError(text);
    await chrome.storage.local.set({
      lastCheckAt: new Date().toISOString(),
      lastError: text
    });
  } finally {
    button.disabled = false;
    button.textContent = "Проверить сейчас";
  }
}

document.getElementById("check").addEventListener("click", runCheck);
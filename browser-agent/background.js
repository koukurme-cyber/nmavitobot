const AVITO_PATH = "/web/3/tariff/cpa/profile?entryPoint=desktop_profile";
const AVITO_URL = "https://www.avito.ru" + AVITO_PATH;
const ALARM_NAME = "avito-cpa-watch";
const DEFAULT_INTERVAL_MIN = 1;

const STOP_CODES = new Set(["CPA_NOT_ENOUGH_ADVANCE", "CPA_VIEWS_PAUSED"]);

async function getLocal(keys) {
  return await chrome.storage.local.get(keys);
}

async function setLocal(values) {
  await chrome.storage.local.set(values);
}

function formatRubKopeks(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(n / 100) + " ₽";
}

function findObjectWithKeys(root) {
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

    if (Array.isArray(value)) {
      for (const item of value) queue.push(item);
    } else {
      for (const item of Object.values(value)) queue.push(item);
    }
  }
  return null;
}

function extractAlertCode(alertValue) {
  if (!alertValue) return null;
  if (typeof alertValue === "string") return alertValue;

  const queue = [alertValue];
  const seen = new Set();
  while (queue.length) {
    const value = queue.shift();
    if (!value || typeof value !== "object") continue;
    if (seen.has(value)) continue;
    seen.add(value);

    for (const v of Object.values(value)) {
      if (typeof v === "string" && v.startsWith("CPA_")) return v;
      if (v && typeof v === "object") queue.push(v);
    }
  }
  return null;
}

function parseAvitoPayload(payload) {
  const holder = findObjectWithKeys(payload);
  if (!holder) {
    throw new Error("В ответе Avito нет advanceThreshold / advanceBalance / cpaAlert");
  }

  const advanceThreshold = holder.advanceThreshold ?? null;
  const advanceBalance = holder.advanceBalance ?? null;
  const cpaAlert = holder.cpaAlert ?? null;
  const cpaAlertCode = extractAlertCode(cpaAlert);

  const thresholdNum = Number(advanceThreshold);
  const balanceNum = Number(advanceBalance);

  const stoppedByBalance =
    Number.isFinite(thresholdNum) &&
    Number.isFinite(balanceNum) &&
    balanceNum < thresholdNum;

  const stoppedByCode = cpaAlertCode ? STOP_CODES.has(cpaAlertCode) : false;

  return {
    advanceThreshold,
    advanceBalance,
    cpaAlert,
    cpaAlertCode,
    stopped: stoppedByBalance || stoppedByCode
  };
}

async function fetchThroughAvitoTab() {
  const tabs = await chrome.tabs.query({url: ["https://www.avito.ru/*"]});
  if (!tabs.length) {
    throw new Error("Нет открытой вкладки Avito");
  }

  let lastError = "";

  for (const tab of tabs) {
    if (!tab.id) continue;

    try {
      const injected = await chrome.scripting.executeScript({
        target: {tabId: tab.id},
        func: async (path) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 12000);

          try {
            const response = await fetch(path, {
              method: "GET",
              credentials: "include",
              cache: "no-store",
              headers: {"Accept": "application/json, text/plain, */*"},
              signal: controller.signal
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
              text: text.slice(0, 500)
            };
          } catch (error) {
            return {
              ok: false,
              status: 0,
              payload: null,
              text: String(error?.message || error)
            };
          } finally {
            clearTimeout(timer);
          }
        },
        args: [AVITO_PATH]
      });

      const reply = injected?.[0]?.result;
      if (reply?.ok) {
        return {...reply, source: "avito-tab"};
      }

      lastError = reply
        ? `HTTP ${reply.status}: ${reply.text || "без текста"}`
        : "вкладка не вернула ответ";
    } catch (error) {
      lastError = String(error?.message || error);
    }
  }

  throw new Error(`Проверка через вкладку Avito не удалась: ${lastError}`);
}

async function fetchFromExtension() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);

  try {
    const response = await fetch(AVITO_URL, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: {"Accept": "application/json, text/plain, */*"},
      signal: controller.signal
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
      text: text.slice(0, 500),
      source: "background"
    };
  } finally {
    clearTimeout(timer);
  }
}

async function fetchAvitoState() {
  try {
    return await fetchThroughAvitoTab();
  } catch (tabError) {
    try {
      const fallback = await fetchFromExtension();
      if (fallback.ok) return fallback;
      throw new Error(`HTTP ${fallback.status}: ${fallback.text || "без текста"}`);
    } catch (backgroundError) {
      throw new Error(
        `${tabError.message} | Фоновая проверка: ${backgroundError.message}`
      );
    }
  }
}

async function sendTelegram(text) {
  const cfg = await getLocal(["telegramBotToken", "telegramChatId"]);
  const token = (cfg.telegramBotToken || "").trim();
  const chatId = (cfg.telegramChatId || "").trim();

  if (!token || !chatId) {
    throw new Error("Не заполнены Telegram Bot Token и Chat ID");
  }

  const response = await fetch(
    `https://api.telegram.org/bot${encodeURIComponent(token)}/sendMessage`,
    {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        chat_id: chatId,
        text,
        disable_web_page_preview: true
      })
    }
  );

  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    throw new Error(payload?.description || `Telegram HTTP ${response.status}`);
  }
}

function stoppedMessage(accountLabel, state) {
  const lines = [
    `🔴 Avito: показы остановлены — ${accountLabel || "аккаунт"}`,
    `Аванс: ${formatRubKopeks(state.advanceBalance)}`,
    `Порог остановки: ${formatRubKopeks(state.advanceThreshold)}`
  ];
  if (state.cpaAlertCode) lines.push(`Состояние: ${state.cpaAlertCode}`);
  return lines.join("\n");
}

function recoveredMessage(accountLabel, state) {
  const lines = [
    `🟢 Avito: показы восстановлены — ${accountLabel || "аккаунт"}`,
    `Аванс: ${formatRubKopeks(state.advanceBalance)}`,
    `Порог остановки: ${formatRubKopeks(state.advanceThreshold)}`
  ];
  if (state.cpaAlertCode) lines.push(`Состояние: ${state.cpaAlertCode}`);
  return lines.join("\n");
}

async function runCheck({manual = false} = {}) {
  const now = new Date().toISOString();
  const cfg = await getLocal(["enabled", "accountLabel", "lastStopped"]);

  if (!manual && cfg.enabled === false) {
    return {ok: true, skipped: true};
  }

  try {
    const raw = await fetchAvitoState();
    const state = parseAvitoPayload(raw.payload);
    const prevStopped =
      typeof cfg.lastStopped === "boolean" ? cfg.lastStopped : null;

    await setLocal({
      lastCheckAt: now,
      lastError: "",
      lastSource: raw.source,
      lastAdvanceThreshold: state.advanceThreshold,
      lastAdvanceBalance: state.advanceBalance,
      lastCpaAlertCode: state.cpaAlertCode,
      lastStopped: state.stopped
    });

    const tg = await getLocal(["telegramBotToken", "telegramChatId"]);
    const telegramReady =
      Boolean((tg.telegramBotToken || "").trim()) &&
      Boolean((tg.telegramChatId || "").trim());

    if (telegramReady) {
      if ((prevStopped === null || prevStopped === false) && state.stopped) {
        await sendTelegram(stoppedMessage(cfg.accountLabel, state));
        await setLocal({lastNotificationAt: now});
      } else if (prevStopped === true && !state.stopped) {
        await sendTelegram(recoveredMessage(cfg.accountLabel, state));
        await setLocal({lastNotificationAt: now});
      }
    }

    return {ok: true, state, source: raw.source};
  } catch (error) {
    const message = String(error?.message || error);
    await setLocal({lastCheckAt: now, lastError: message});
    return {ok: false, error: message};
  }
}

async function resetAlarm() {
  const cfg = await getLocal(["intervalMinutes"]);
  const interval = Math.max(1, Number(cfg.intervalMinutes) || DEFAULT_INTERVAL_MIN);

  await chrome.alarms.clear(ALARM_NAME);
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: 0.1,
    periodInMinutes: interval
  });
}

chrome.runtime.onInstalled.addListener(async () => {
  const current = await getLocal(["enabled", "accountLabel", "intervalMinutes"]);
  const defaults = {};
  if (typeof current.enabled !== "boolean") defaults.enabled = true;
  if (!current.accountLabel) defaults.accountLabel = "Аккаунт";
  if (!current.intervalMinutes) defaults.intervalMinutes = DEFAULT_INTERVAL_MIN;
  if (Object.keys(defaults).length) await setLocal(defaults);
  await resetAlarm();
});

chrome.runtime.onStartup.addListener(resetAlarm);

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) await runCheck();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message?.type === "RUN_CHECK") {
      sendResponse(await runCheck({manual: true}));
      return;
    }

    if (message?.type === "TEST_TELEGRAM") {
      try {
        const cfg = await getLocal(["accountLabel"]);
        await sendTelegram(
          `Тест Avito CPA Watch — ${cfg.accountLabel || "аккаунт"}\nСвязь с Telegram работает.`
        );
        sendResponse({ok: true});
      } catch (error) {
        sendResponse({ok: false, error: String(error?.message || error)});
      }
      return;
    }

    if (message?.type === "RESET_ALARM") {
      await resetAlarm();
      sendResponse({ok: true});
      return;
    }

    sendResponse({ok: false, error: "Неизвестная команда"});
  })();

  return true;
});

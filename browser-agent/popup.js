function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(n / 100) + " ₽";
}

function when(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString("ru-RU");
  } catch (_) {
    return value;
  }
}

async function refresh() {
  const s = await chrome.storage.local.get([
    "accountLabel",
    "lastAdvanceBalance",
    "lastAdvanceThreshold",
    "lastCpaAlertCode",
    "lastStopped",
    "lastSource",
    "lastCheckAt",
    "lastError"
  ]);

  document.getElementById("accountLabel").textContent = s.accountLabel || "—";
  document.getElementById("balance").textContent = money(s.lastAdvanceBalance);
  document.getElementById("threshold").textContent = money(s.lastAdvanceThreshold);
  document.getElementById("state").textContent =
    s.lastStopped === true
      ? "Показы остановлены"
      : s.lastStopped === false
        ? (s.lastCpaAlertCode || "Показы активны")
        : "—";
  document.getElementById("source").textContent = s.lastSource || "—";
  document.getElementById("checkedAt").textContent = when(s.lastCheckAt);

  const error = document.getElementById("error");
  if (s.lastError) {
    error.textContent = s.lastError;
    error.classList.remove("hidden");
  } else {
    error.textContent = "";
    error.classList.add("hidden");
  }
}

function sendMessageWithTimeout(message, timeoutMs = 18000) {
  return Promise.race([
    chrome.runtime.sendMessage(message),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Проверка превысила 18 секунд")), timeoutMs)
    )
  ]);
}

document.getElementById("checkNow").addEventListener("click", async () => {
  const button = document.getElementById("checkNow");
  button.disabled = true;
  button.textContent = "Проверяю…";

  try {
    await sendMessageWithTimeout({type: "RUN_CHECK"});
  } catch (error) {
    await chrome.storage.local.set({
      lastCheckAt: new Date().toISOString(),
      lastError: String(error?.message || error)
    });
  }

  await refresh();
  button.disabled = false;
  button.textContent = "Проверить сейчас";
});

document.getElementById("openOptions").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

refresh();

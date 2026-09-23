const ids = [
  "enabled",
  "accountLabel",
  "telegramBotToken",
  "telegramChatId",
  "intervalMinutes"
];

function showMessage(text, isError = false) {
  const box = document.getElementById("message");
  box.textContent = text;
  box.classList.remove("hidden", "error");
  if (isError) box.classList.add("error");
}

async function load() {
  const s = await chrome.storage.local.get(ids);
  document.getElementById("enabled").checked = s.enabled !== false;
  document.getElementById("accountLabel").value = s.accountLabel || "Агрегаты";
  document.getElementById("telegramBotToken").value = s.telegramBotToken || "";
  document.getElementById("telegramChatId").value = s.telegramChatId || "";
  document.getElementById("intervalMinutes").value =
    Math.max(1, Number(s.intervalMinutes) || 1);
}

async function save() {
  const values = {
    enabled: document.getElementById("enabled").checked,
    accountLabel: document.getElementById("accountLabel").value.trim() || "Аккаунт",
    telegramBotToken: document.getElementById("telegramBotToken").value.trim(),
    telegramChatId: document.getElementById("telegramChatId").value.trim(),
    intervalMinutes: Math.max(
      1,
      Number(document.getElementById("intervalMinutes").value) || 1
    )
  };

  await chrome.storage.local.set(values);
  await chrome.runtime.sendMessage({type: "RESET_ALARM"});
  showMessage("Сохранено.");
}

document.getElementById("save").addEventListener("click", save);

document.getElementById("testTelegram").addEventListener("click", async () => {
  await save();
  const result = await chrome.runtime.sendMessage({type: "TEST_TELEGRAM"});
  if (result?.ok) showMessage("Тестовое сообщение отправлено.");
  else showMessage(result?.error || "Ошибка Telegram", true);
});

document.getElementById("checkNow").addEventListener("click", async () => {
  await save();
  const result = await chrome.runtime.sendMessage({type: "RUN_CHECK"});
  if (result?.ok) {
    const state = result.state || {};
    const source = result.source || "—";
    showMessage(
      `Avito отвечает. Источник: ${source}. ` +
      `advanceBalance=${state.advanceBalance ?? "—"}, ` +
      `advanceThreshold=${state.advanceThreshold ?? "—"}, ` +
      `cpaAlert=${state.cpaAlertCode ?? "null"}`
    );
  } else {
    showMessage(result?.error || "Ошибка проверки Avito", true);
  }
});

load();

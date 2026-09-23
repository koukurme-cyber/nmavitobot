chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "AVITO_CPA_FETCH") return;

  (async () => {
    try {
      const response = await fetch(
        "/web/3/tariff/cpa/profile?entryPoint=desktop_profile",
        {
          method: "GET",
          credentials: "include",
          headers: {
            "Accept": "application/json, text/plain, */*"
          },
          cache: "no-store"
        }
      );

      const text = await response.text();
      let payload = null;
      try {
        payload = JSON.parse(text);
      } catch (_) {}

      sendResponse({
        ok: response.ok,
        status: response.status,
        payload,
        text: text.slice(0, 500)
      });
    } catch (error) {
      sendResponse({
        ok: false,
        status: 0,
        payload: null,
        text: String(error?.message || error)
      });
    }
  })();

  return true;
});

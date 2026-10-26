(function () {
  "use strict";

  const STORAGE_KEY = "agent-community.locale";
  const DEFAULT_LOCALE = "en";
  const SUPPORTED_LOCALES = ["en"];
  const resources = window.AgentCommunityLocales || {};

  function normalizeLocale(value) {
    return String(value || "").toLowerCase().startsWith("en") ? "en" : null;
  }

  function initialLocale() {
    return DEFAULT_LOCALE;
  }

  let locale = initialLocale();

  function t(key, values = {}) {
    const message = resources[locale]?.[key]
      ?? resources[DEFAULT_LOCALE]?.[key]
      ?? key;
    return String(message).replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) => (
      Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match
    ));
  }

  function setLocale(value) {
    const next = normalizeLocale(value);
    if (!next || !SUPPORTED_LOCALES.includes(next)) return false;
    locale = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (_) {
      // The selected locale still applies for this page when storage is unavailable.
    }
    applyDocument();
    return true;
  }

  function applyDocument(root = document) {
    document.documentElement.lang = locale;
    root.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
      element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
    });
    document.title = t("app.subtitle");
    document.body?.classList.add("i18n-ready");
  }

  window.AgentCommunityI18n = {
    applyDocument,
    getLocale: () => locale,
    setLocale,
    supportedLocales: [...SUPPORTED_LOCALES],
    t,
  };
})();

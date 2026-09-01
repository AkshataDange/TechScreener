const AdminApp = (() => {
  const API = "";
  const TOKEN_KEY = "techscreen_admin_token";
  let authToken = localStorage.getItem(TOKEN_KEY) || "";
  let currentAdmin = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function setMessage(targetId, text, type = "error") {
    const el = document.getElementById(targetId);
    if (!el) {
      return;
    }
    el.textContent = text;
    el.className = `message ${type}`;
  }

  function clearMessage(targetId) {
    const el = document.getElementById(targetId);
    if (!el) {
      return;
    }
    el.textContent = "";
    el.className = "message";
  }

  function authHeaders(extra = {}) {
    return authToken ? { ...extra, Authorization: `Bearer ${authToken}` } : { ...extra };
  }

  async function readJsonResponse(response) {
    const raw = await response.text();
    if (!raw) {
      return {};
    }

    try {
      return JSON.parse(raw);
    } catch (_error) {
      if (!response.ok) {
        throw new Error(raw.trim() || "Request failed");
      }
      throw new Error("Server returned an invalid JSON response.");
    }
  }

  async function fetchJson(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers: authHeaders(options.headers || {}),
    });
    const data = await readJsonResponse(response);
    if (!response.ok) {
      throw new Error(data.detail || "Request failed");
    }
    return data;
  }

  async function loginAdmin(email, password) {
    const data = await fetchJson("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (data.user.role !== "admin") {
      throw new Error("This account is not an admin.");
    }
    authToken = data.token;
    currentAdmin = data.user;
    localStorage.setItem(TOKEN_KEY, authToken);
    return data.user;
  }

  async function restoreAdmin() {
    if (!authToken) {
      return null;
    }
    const data = await fetchJson("/auth/me");
    if (data.role !== "admin") {
      throw new Error("This account is not an admin.");
    }
    currentAdmin = data;
    return data;
  }

  function getCurrentAdmin() {
    return currentAdmin;
  }

  function logout() {
    authToken = "";
    currentAdmin = null;
    localStorage.removeItem(TOKEN_KEY);
    window.location.replace("/admin");
  }

  function bindLogout(buttonId = "logout-btn") {
    const button = document.getElementById(buttonId);
    if (button) {
      button.addEventListener("click", logout);
    }
  }

  function formatDateTime(value) {
    if (!value) {
      return "Not available";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString();
  }

  function getQueryParam(name) {
    return new URLSearchParams(window.location.search).get(name) || "";
  }

  function setAdminSummary(targetId = "admin-summary") {
    const el = document.getElementById(targetId);
    if (el && currentAdmin) {
      el.textContent = `Signed in as ${currentAdmin.name} (${currentAdmin.email})`;
    }
  }

  async function requireAdmin({ loginPage = false } = {}) {
    try {
      const admin = await restoreAdmin();
      if (loginPage) {
        if (admin) {
          document.getElementById("login-page")?.classList.remove("active");
          document.getElementById("portal-page")?.classList.add("active");
        } else {
          document.getElementById("login-page")?.classList.add("active");
          document.getElementById("portal-page")?.classList.remove("active");
        }
      }
      setAdminSummary();
      bindLogout();
      return currentAdmin;
    } catch (_error) {
      if (loginPage) {
        document.getElementById("login-page")?.classList.add("active");
        document.getElementById("portal-page")?.classList.remove("active");
        return null;
      }
      logout();
      return null;
    }
  }

  return {
    bindLogout,
    clearMessage,
    escapeHtml,
    fetchJson,
    formatDateTime,
    getCurrentAdmin,
    getQueryParam,
    loginAdmin,
    logout,
    requireAdmin,
    setAdminSummary,
    setMessage,
  };
})();

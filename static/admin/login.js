      (async function () {
        function esc(v) {
          return String(v).replace(
            /[&<>"']/g,
            (c) =>
              ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
          );
        }
        const msg = document.getElementById("msg");
        const r = await fetch("/api/auth/me", { credentials: "include" });
        const me = await r.json().catch(() => ({}));
        if (!me.auth_enabled) {
          msg.innerHTML =
            '<div class="msg ok">Authentication is disabled. <a href="/admin/">Continue to admin</a>.</div>';
          document.getElementById("login-form").style.display = "none";
          return;
        }
        if (me.logged_in) {
          window.location.replace("/admin/");
          return;
        }
        document.getElementById("login-form").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          msg.innerHTML = "";
          const password = document.getElementById("pw").value;
          const res = await fetch("/api/auth/login", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            msg.innerHTML =
              '<div class="msg err">' +
              esc(typeof data.detail === "string" ? data.detail : "Sign-in failed") +
              "</div>";
            return;
          }
          window.location.replace("/admin/");
        });
      })();
    

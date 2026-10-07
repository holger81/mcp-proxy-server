      (async function () {
        function apiFetch(input, init) {
          const o = init ? { ...init } : {};
          o.credentials = "include";
          return fetch(input, o);
        }

        // PLAN 7.1: every dynamic value interpolated into innerHTML must go
        // through esc() — server ids, API error details and upstream tool
        // names are attacker-controlled (a malicious upstream can name a
        // tool `<img src=x onerror=…>`).
        const _ESC_MAP = {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        };
        function esc(v) {
          return String(v).replace(/[&<>"']/g, (c) => _ESC_MAP[c]);
        }

        const _meRes = await apiFetch("/api/auth/me");
        const _me = await _meRes.json().catch(() => ({}));
        if (_me.auth_enabled && !_me.logged_in) {
          window.location.replace("/admin/login.html");
          return;
        }

        if (!_me.auth_enabled) {
          const ban = document.getElementById("auth-off-banner");
          ban.classList.remove("hidden");
          // Single template literal (no `+`): provably static markup.
          ban.innerHTML =
            `<strong>No admin password is set.</strong> Anyone who can reach this service can use the admin UI and API. Set <code>MCP_PROXY_ADMIN_PASSWORD</code> and <code>MCP_PROXY_SESSION_SECRET</code> (≥16 random chars), or Docker secret files via <code>MCP_PROXY_ADMIN_PASSWORD_FILE</code> and <code>MCP_PROXY_SESSION_SECRET_FILE</code>, then restart. Check container logs for “Authentication is enabled”. Then use <a href="/admin/login.html">Sign in</a>.`;
        }

        const api = "/api/servers";
        const apiClients = "/api/clients";
        const apiDomains = "/api/domains";
        const apiLogs = "/api/logs";
        const apiLlmPreview = "/api/mcp-llm-preview";
        const apiLive = "/api/mcp-live";
        const apiMailMcp = "/api/mail-mcp";
        const apiPortainerMcp = "/api/portainer-mcp";

        let cachedDomains = [];

        async function loadDomainsList() {
          const res = await apiFetch(apiDomains);
          if (!res.ok) {
            cachedDomains = [];
            return;
          }
          cachedDomains = await res.json();
          fillAllDomainSelects();
        }

        function fillAllDomainSelects() {
          const selIds = ["wiz-domain", "ms-domain", "f-domain", "e-domain"];
          for (const sid of selIds) {
            const el = document.getElementById(sid);
            if (!el) continue;
            const cur = el.value;
            el.replaceChildren();
            for (const d of cachedDomains) {
              const opt = document.createElement("option");
              opt.value = d.id;
              opt.textContent = d.label + " (" + d.id + ")";
              el.appendChild(opt);
            }
            if (cur && [...el.options].some((o) => o.value === cur)) {
              el.value = cur;
            } else if (el.options.length) {
              el.value = el.options[0].value;
            }
          }
        }

        await loadDomainsList();

        const wizGo = document.getElementById("wiz-go");
        const wizLog = document.getElementById("wiz-log");
        const wizMsg = document.getElementById("wiz-msg");

        const tbody = document.getElementById("srv-body");
        const table = document.getElementById("srv-table");
        const empty = document.getElementById("list-empty");
        const httpForm = document.getElementById("add-http-form");
        const formMsg = document.getElementById("form-msg");
        const inspectOut = document.getElementById("inspect-out");
        const panelReg = document.getElementById("panel-register");
        const panelExp = document.getElementById("panel-explore");
        const panelClients = document.getElementById("panel-clients");
        const panelLive = document.getElementById("panel-live");
        const panelDomains = document.getElementById("panel-domains");
        const panelLogs = document.getElementById("panel-logs");
        const panelLlm = document.getElementById("panel-llm");
        const liveBody = document.getElementById("live-body");
        const liveTable = document.getElementById("live-table");
        const liveEmpty = document.getElementById("live-empty");
        const liveMsg = document.getElementById("live-msg");

        let livePollId = null;
        const LIVE_POLL_MS = 1000;

        function stopLivePolling() {
          if (livePollId != null) {
            clearInterval(livePollId);
            livePollId = null;
          }
        }

        function startLivePolling() {
          stopLivePolling();
          livePollId = setInterval(() => {
            if (!panelLive.classList.contains("hidden")) {
              loadLive({ silent: true });
            }
          }, LIVE_POLL_MS);
        }

        function fmtMs(ms) {
          ms = Math.max(0, Number(ms) || 0);
          if (ms < 1000) return ms + "ms";
          const s = Math.floor(ms / 1000);
          if (s < 60) return s + "s";
          const m = Math.floor(s / 60);
          if (m < 60) return m + "m";
          const h = Math.floor(m / 60);
          return h + "h";
        }

        async function loadLive(opts) {
          opts = opts || {};
          const silent = opts.silent === true;
          liveMsg.innerHTML = "";
          if (!silent) {
            liveEmpty.classList.remove("hidden");
            liveEmpty.textContent = "Loading…";
            liveTable.classList.add("hidden");
          }
          let winS = parseInt(document.getElementById("live-window").value, 10);
          if (!Number.isFinite(winS) || winS < 5) winS = 90;
          if (winS > 3600) winS = 3600;
          const res = await apiFetch(apiLive + "?active_within_ms=" + encodeURIComponent(String(winS * 1000)));
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            if (!silent) {
              liveEmpty.textContent =
                typeof data.detail === "string" ? data.detail : "Failed to load (" + res.status + ").";
            }
            return;
          }
          const clients = data.clients || [];
          liveBody.replaceChildren();
          if (!clients.length) {
            liveTable.classList.add("hidden");
            liveEmpty.classList.remove("hidden");
            liveEmpty.textContent = "(no recent /mcp clients)";
            return;
          }
          liveEmpty.classList.add("hidden");
          liveTable.classList.remove("hidden");
          for (const c of clients) {
            const tr = document.createElement("tr");
            const tdS = document.createElement("td");
            tdS.textContent = c.session_id || "—";
            const tdC = document.createElement("td");
            tdC.textContent = c.api_client_label ? (c.api_client_label + " (" + (c.api_client_id || "—") + ")") : (c.api_client_id || "—");
            const tdP = document.createElement("td");
            const preP = document.createElement("pre");
            preP.style.margin = "0";
            preP.style.background = "transparent";
            preP.style.padding = "0";
            preP.textContent = (c.peer || "—") + (c.user_agent ? ("\n" + c.user_agent) : "");
            tdP.appendChild(preP);
            const tdI = document.createElement("td");
            tdI.textContent = fmtMs(c.idle_ms);
            const tdR = document.createElement("td");
            const calls = c.active_calls || [];
            const recent = c.recent_calls || [];
            const lines = [];
            for (const x of calls) {
              lines.push("▶ " + x.tool + " (" + fmtMs(x.running_ms) + ")");
            }
            for (const x of recent) {
              lines.push("✓ " + x.tool + " (" + fmtMs(x.duration_ms) + ", " + fmtMs(x.ago_ms) + " ago)");
            }
            if (!lines.length) {
              tdR.textContent = "—";
            } else {
              const pre = document.createElement("pre");
              pre.style.margin = "0";
              pre.style.background = "transparent";
              pre.style.padding = "0";
              pre.textContent = lines.join("\n");
              tdR.appendChild(pre);
            }
            tr.append(tdS, tdC, tdP, tdI, tdR);
            liveBody.appendChild(tr);
          }
        }
        const clientsBody = document.getElementById("clients-body");
        const clientsTable = document.getElementById("clients-table");
        const clientsEmpty = document.getElementById("clients-empty");
        const clientMsg = document.getElementById("client-msg");
        const btnLogout = document.getElementById("btn-logout");
        if (_me.auth_enabled) btnLogout.classList.remove("hidden");
        btnLogout.addEventListener("click", async () => {
          await apiFetch("/api/auth/logout", { method: "POST" });
          window.location.href = "/admin/login.html";
        });
        const editDialog = document.getElementById("edit-dialog");
        const editForm = document.getElementById("edit-form");
        const editMsg = document.getElementById("edit-msg");
        const eBlkHttp = document.getElementById("e-blk-http");
        const eBlkStdio = document.getElementById("e-blk-stdio");

        function parseJsonObject(text, label) {
          const t = (text || "").trim();
          if (!t) return {};
          try {
            const v = JSON.parse(t);
            if (v === null || typeof v !== "object" || Array.isArray(v)) {
              throw new Error(label + " must be a JSON object");
            }
            return v;
          } catch (e) {
            throw new Error((e && e.message) || "Invalid JSON");
          }
        }

        function targetCell(s) {
          if (s.type === "http") return s.url || "—";
          const c = s.command;
          let line = Array.isArray(c) ? c.join(" ") : "—";
          if (s.type === "stdio" && s.stdio_node_inspect) {
            const kind = s.stdio_node_inspect_brk ? "inspect-brk" : "inspect";
            line +=
              "\n→ spawns with --" + kind + "=0.0.0.0:9229 · attach at localhost:9229 (Compose publishes 9229 only)";
          }
          return line;
        }

        function httpMode(s) {
          if (s.type !== "http") return "—";
          const t = s.http_transport || "streamable-http";
          if (t === "sse") return "SSE";
          return "Streamable";
        }

        function commandToString(cmd) {
          if (!cmd) return "";
          if (Array.isArray(cmd)) return cmd.join(" ");
          return String(cmd);
        }

        async function fetchStdioStatus(serverId) {
          const res = await apiFetch(api + "/" + encodeURIComponent(serverId) + "/stdio-package-status");
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            return { ok: false, detail: typeof data.detail === "string" ? data.detail : "status lookup failed (" + res.status + ")" };
          }
          return { ok: true, data };
        }

        function formatStdioStatus(data) {
          if (!data || !data.managed) return "Not managed (manual stdio)";
          const eco = data.ecosystem || "—";
          const spec = data.package_spec || "—";
          const cur = data.installed_version || "?";
          const latest = data.latest_version || "?";
          const verPart =
            data.installed_npm_name && data.installed_version
              ? data.installed_npm_name + " " + cur
              : cur;
          if (data.update_available) {
            return eco + " " + spec + "\n" + verPart + " -> " + latest + " (update available)";
          }
          return eco + " " + spec + "\n" + verPart + " (latest " + latest + ")";
        }

        function isMailMcpCommand(s) {
          const c = s.command;
          if (!Array.isArray(c) || !c.length) return false;
          const base = String(c[0])
            .replace(/\\/g, "/")
            .split("/")
            .pop()
            .toLowerCase();
          return base === "mail-mcp" || base === "mail-mcp.exe";
        }

        function isPortainerMcpCommand(s) {
          const c = s.command;
          if (!Array.isArray(c) || !c.length) return false;
          const base = String(c[0])
            .replace(/\\/g, "/")
            .split("/")
            .pop()
            .toLowerCase();
          return base === "portainer-mcp-enhanced" || base === "portainer-mcp-enhanced.exe";
        }

        async function upgradeStdioServer(serverId, pkgPre) {
          pkgPre.textContent = "Upgrading…";
          const res = await apiFetch(api + "/" + encodeURIComponent(serverId) + "/upgrade-stdio-package", {
            method: "POST",
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok || !data.ok) {
            const msg = typeof data.detail === "string" ? data.detail : "Upgrade failed (" + res.status + ")";
            pkgPre.textContent = "Upgrade failed: " + msg;
            return;
          }
          const status = data.status || null;
          pkgPre.textContent = status ? formatStdioStatus(status) : "Upgraded.";
          if (data.log) {
            inspectOut.classList.remove("hidden");
            inspectOut.textContent = data.log;
          }
        }

        function openEdit(s) {
          editMsg.innerHTML = "";
          document.getElementById("e-id").value = s.id;
          document.getElementById("e-name").value = s.display_name || "";
          fillAllDomainSelects();
          document.getElementById("e-domain").value = s.domain || "default";
          document.getElementById("e-enabled").checked = !!s.enabled;
          editForm.querySelectorAll('input[name="e-type"]').forEach((el) => {
            el.checked = el.value === s.type;
          });
          eBlkHttp.classList.toggle("hidden", s.type !== "http");
          eBlkStdio.classList.toggle("hidden", s.type !== "stdio");
          const ht = s.http_transport === "sse" ? "sse" : "streamable-http";
          editForm.querySelectorAll('input[name="e-http_transport"]').forEach((el) => {
            el.checked = el.value === ht;
          });
          document.getElementById("e-url").value = s.url || "";
          document.getElementById("e-headers").value = JSON.stringify(s.headers && Object.keys(s.headers).length ? s.headers : {}, null, 2);
          document.getElementById("e-cmd").value = commandToString(s.command);
          document.getElementById("e-node-inspect").checked = !!s.stdio_node_inspect;
          document.getElementById("e-node-inspect-brk").checked = !!s.stdio_node_inspect_brk;
          document.getElementById("e-cwd").value = s.cwd || "";
          document.getElementById("e-env").value = JSON.stringify(s.env && Object.keys(s.env).length ? s.env : {}, null, 2);
          document.getElementById("e-llm").value = s.llm_context || "";
          editDialog.showModal();
        }

        document.getElementById("edit-cancel").addEventListener("click", () => editDialog.close());

        document.querySelectorAll('input[name="e-type"]').forEach((r) => {
          r.addEventListener("change", () => {
            const t = editForm.querySelector('input[name="e-type"]:checked').value;
            eBlkHttp.classList.toggle("hidden", t !== "http");
            eBlkStdio.classList.toggle("hidden", t !== "stdio");
          });
        });

        editForm.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          editMsg.innerHTML = "";
          const id = document.getElementById("e-id").value.trim();
          const display_name = document.getElementById("e-name").value.trim() || null;
          const type = editForm.querySelector('input[name="e-type"]:checked').value;
          const enabled = document.getElementById("e-enabled").checked;
          let body;
          try {
            const domain = document.getElementById("e-domain").value.trim() || "default";
            if (type === "http") {
              const url = document.getElementById("e-url").value.trim();
              const headers = parseJsonObject(document.getElementById("e-headers").value, "Headers");
              const http_transport = editForm.querySelector('input[name="e-http_transport"]:checked').value;
              body = {
                id,
                domain,
                type: "http",
                enabled,
                display_name,
                url,
                headers,
                http_transport,
                llm_context: document.getElementById("e-llm").value,
              };
            } else {
              const cmd = document.getElementById("e-cmd").value.trim();
              const cwd = document.getElementById("e-cwd").value.trim() || null;
              const env = parseJsonObject(document.getElementById("e-env").value, "Environment");
              body = {
                id,
                domain,
                type: "stdio",
                enabled,
                display_name,
                command: cmd,
                cwd,
                env,
                llm_context: document.getElementById("e-llm").value,
                stdio_node_inspect: document.getElementById("e-node-inspect").checked,
                stdio_node_inspect_brk: document.getElementById("e-node-inspect-brk").checked,
                stdio_node_inspect_port: 9229,
              };
            }
          } catch (e) {
            editMsg.innerHTML =
              '<div class="msg err">' + esc(e.message || String(e)) + "</div>";
            return;
          }
          const res = await apiFetch(api + "/" + encodeURIComponent(id), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const detail = data.detail;
            const msg =
              typeof detail === "string"
                ? detail
                : Array.isArray(detail)
                  ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
                  : JSON.stringify(data);
            editMsg.innerHTML = '<div class="msg err">' + esc(msg) + "</div>";
            return;
          }
          editDialog.close();
          loadServers();
        });

        async function runInspect(serverId, kind) {
          inspectOut.classList.remove("hidden");
          inspectOut.textContent = "Loading…";
          const url = api + "/" + encodeURIComponent(serverId) + "/inspect?kind=" + encodeURIComponent(kind);
          const res = await apiFetch(url);
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            inspectOut.textContent = JSON.stringify({ error: data.detail || data }, null, 2);
            return;
          }
          inspectOut.textContent = JSON.stringify(data, null, 2);
        }

        async function loadServers() {
          const res = await apiFetch(api);
          if (!res.ok) {
            empty.classList.remove("hidden");
            empty.textContent = "Could not load servers (" + res.status + ").";
            table.classList.add("hidden");
            return;
          }
          const list = await res.json();
          tbody.replaceChildren();
          if (!list.length) {
            empty.classList.remove("hidden");
            table.classList.add("hidden");
            return;
          }
          empty.classList.add("hidden");
          table.classList.remove("hidden");
          for (const s of list) {
            const tr = document.createElement("tr");
            const tdId = document.createElement("td");
            tdId.textContent = s.id;
            const tdDom = document.createElement("td");
            tdDom.textContent = s.domain || "default";
            const tdType = document.createElement("td");
            tdType.textContent = s.type;
            const tdHttp = document.createElement("td");
            tdHttp.textContent = httpMode(s);
            const tdTgt = document.createElement("td");
            const pre = document.createElement("pre");
            pre.style.margin = "0";
            pre.style.background = "transparent";
            pre.style.padding = "0";
            pre.textContent = targetCell(s);
            tdTgt.appendChild(pre);
            const tdPkg = document.createElement("td");
            const pkgPre = document.createElement("pre");
            pkgPre.style.margin = "0";
            pkgPre.style.background = "transparent";
            pkgPre.style.padding = "0";
            pkgPre.textContent = s.type === "stdio" ? "Loading…" : "—";
            tdPkg.appendChild(pkgPre);
            const tdIn = document.createElement("td");
            const kinds = [
              ["tools", "Tools"],
              ["resources", "Resources"],
              ["prompts", "Prompts"],
              ["capabilities", "Capabilities"],
            ];
            for (const [kind, label] of kinds) {
              const b = document.createElement("button");
              b.type = "button";
              b.className = "small secondary";
              b.textContent = label;
              b.addEventListener("click", () => runInspect(s.id, kind));
              tdIn.appendChild(b);
            }
            const tdAct = document.createElement("td");
            const tog = document.createElement("button");
            tog.type = "button";
            tog.className = "small secondary";
            tog.textContent = s.enabled ? "Disable" : "Enable";
            tog.addEventListener("click", async () => {
              const next = !s.enabled;
              const body = { ...s, enabled: next };
              const r = await apiFetch(api + "/" + encodeURIComponent(s.id), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
              });
              const data = await r.json().catch(() => ({}));
              if (!r.ok) {
                alert(typeof data.detail === "string" ? data.detail : "Update failed: " + r.status);
                return;
              }
              loadServers();
            });
            tdAct.appendChild(tog);
            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "small secondary";
            editBtn.textContent = "Edit";
            editBtn.addEventListener("click", () => openEdit(s));
            tdAct.appendChild(editBtn);
            let stdioWrap = null;
            if (s.type === "stdio") {
              stdioWrap = document.createElement("span");
              stdioWrap.style.display = "inline-flex";
              stdioWrap.style.flexWrap = "wrap";
              stdioWrap.style.gap = "0.25rem";
              stdioWrap.style.alignItems = "center";
              tdAct.appendChild(stdioWrap);
            }
            const del = document.createElement("button");
            del.type = "button";
            del.className = "danger";
            del.textContent = "Remove";
            del.addEventListener("click", async () => {
              if (!confirm("Remove server “" + s.id + "”?")) return;
              const r = await apiFetch(api + "/" + encodeURIComponent(s.id), { method: "DELETE" });
              if (!r.ok) {
                alert("Delete failed: " + r.status);
                return;
              }
              loadServers();
            });
            tdAct.appendChild(del);
            tr.append(tdId, tdDom, tdType, tdHttp, tdTgt, tdPkg, tdIn, tdAct);
            tbody.appendChild(tr);
            if (s.type === "stdio" && stdioWrap) {
              fetchStdioStatus(s.id).then((st) => {
                if (!st.ok) {
                  pkgPre.textContent = "Check failed: " + st.detail;
                  return;
                }
                pkgPre.textContent = formatStdioStatus(st.data);
                stdioWrap.replaceChildren();
                const managed = st.data && st.data.managed;
                if (managed) {
                  const checkBtn = document.createElement("button");
                  checkBtn.type = "button";
                  checkBtn.className = "small secondary";
                  checkBtn.textContent = "Check update";
                  checkBtn.addEventListener("click", async () => {
                    pkgPre.textContent = "Checking…";
                    const st2 = await fetchStdioStatus(s.id);
                    if (!st2.ok) {
                      pkgPre.textContent = "Check failed: " + st2.detail;
                      return;
                    }
                    pkgPre.textContent = formatStdioStatus(st2.data);
                  });
                  stdioWrap.appendChild(checkBtn);
                  const upBtn = document.createElement("button");
                  upBtn.type = "button";
                  upBtn.className = "small secondary";
                  upBtn.textContent = "Upgrade";
                  upBtn.addEventListener("click", async () => {
                    if (!confirm("Upgrade stdio server “" + s.id + "” to latest package version?")) return;
                    await upgradeStdioServer(s.id, pkgPre);
                    loadServers();
                  });
                  stdioWrap.appendChild(upBtn);
                } else if (isMailMcpCommand(s)) {
                  const ub = document.createElement("button");
                  ub.type = "button";
                  ub.className = "small secondary";
                  ub.textContent = "Update mail-mcp binary";
                  ub.addEventListener("click", async () => {
                    const ver = prompt("mail-mcp release tag or latest:", "latest");
                    if (ver === null) return;
                    const vt = ver.trim();
                    if (!vt) {
                      alert("Enter a version or latest.");
                      return;
                    }
                    pkgPre.textContent = "Downloading mail-mcp…";
                    const res = await apiFetch(apiMailMcp + "/update", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ version: vt }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                      let detail = "";
                      const d = data.detail;
                      if (typeof d === "string") detail = d;
                      else if (d && typeof d === "object" && d.output) detail = d.output;
                      else detail = JSON.stringify(data, null, 2);
                      pkgPre.textContent = "mail-mcp update failed.";
                      inspectOut.classList.remove("hidden");
                      inspectOut.textContent = detail;
                      return;
                    }
                    const baseLine = formatStdioStatus(st.data);
                    pkgPre.textContent =
                      baseLine + "\n\n--- install log ---\n" + (data.output || "").trim();
                    inspectOut.classList.remove("hidden");
                    inspectOut.textContent = JSON.stringify(data, null, 2);
                  });
                  stdioWrap.appendChild(ub);
                } else if (isPortainerMcpCommand(s)) {
                  const ub = document.createElement("button");
                  ub.type = "button";
                  ub.className = "small secondary";
                  ub.textContent = "Update portainer-mcp binary";
                  ub.addEventListener("click", async () => {
                    const ver = prompt("portainer-mcp-enhanced release tag or latest:", "latest");
                    if (ver === null) return;
                    const vt = ver.trim();
                    if (!vt) {
                      alert("Enter a version or latest.");
                      return;
                    }
                    pkgPre.textContent = "Downloading portainer-mcp-enhanced…";
                    const res = await apiFetch(apiPortainerMcp + "/update", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ version: vt }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                      let detail = "";
                      const d = data.detail;
                      if (typeof d === "string") detail = d;
                      else if (d && typeof d === "object" && d.output) detail = d.output;
                      else detail = JSON.stringify(data, null, 2);
                      pkgPre.textContent = "portainer-mcp update failed.";
                      inspectOut.classList.remove("hidden");
                      inspectOut.textContent = detail;
                      return;
                    }
                    const baseLine = formatStdioStatus(st.data);
                    pkgPre.textContent =
                      baseLine + "\n\n--- install log ---\n" + (data.output || "").trim();
                    inspectOut.classList.remove("hidden");
                    inspectOut.textContent = JSON.stringify(data, null, 2);
                  });
                  stdioWrap.appendChild(ub);
                }
              });
            }
          }
        }

        const CLIENT_LLM_FIELDS = [
          { key: "tool_search_max_matches", label: "Search max matches", type: "number" },
          { key: "tool_domain_default_limit", label: "Domain page default", type: "number" },
          { key: "tool_domain_max_limit", label: "Domain page max", type: "number" },
          { key: "tool_description_max_chars", label: "Description max chars", type: "number" },
          { key: "tool_server_llm_context_max_chars", label: "Server LLM context max", type: "number" },
          { key: "tool_input_schema_max_chars", label: "Input schema max chars", type: "number" },
          { key: "call_tool_response_page_chars", label: "Call response page chars", type: "number" },
          { key: "call_tool_response_text_max_chars", label: "Call response hard max (legacy)", type: "number" },
          { key: "instructions_max_chars", label: "Instructions max chars", type: "number" },
          { key: "tool_discovery_compact_json", label: "Compact discovery JSON", type: "checkbox" },
        ];

        let clientToolCatalogCache = null;
        let clientConfigEditingId = null;

        function hideClientConfigPanel() {
          clientConfigEditingId = null;
          document.getElementById("client-config-panel").classList.add("hidden");
          document.getElementById("client-config-msg").innerHTML = "";
        }

        async function loadClientToolCatalog() {
          if (clientToolCatalogCache) return clientToolCatalogCache;
          const res = await apiFetch(apiClients + "/tool-catalog");
          if (!res.ok) throw new Error("tool catalog " + res.status);
          clientToolCatalogCache = await res.json();
          return clientToolCatalogCache;
        }

        function buildClientLlmLimitsForm(globalLimits, clientLimits) {
          const form = document.getElementById("client-llm-limits-form");
          form.replaceChildren();
          const lim = clientLimits || {};
          const glob = globalLimits || {};
          for (const f of CLIENT_LLM_FIELDS) {
            const wrap = document.createElement("div");
            const lab = document.createElement("label");
            lab.setAttribute("for", "cll-" + f.key);
            const defHint =
              f.type === "checkbox"
                ? glob[f.key] === true
                  ? "true"
                  : "false"
                : glob[f.key] !== undefined && glob[f.key] !== null
                  ? String(glob[f.key])
                  : "—";
            lab.textContent = f.label + " ";
            const hint = document.createElement("span");
            hint.className = "hint";
            hint.textContent = "(default: " + defHint + ")";
            lab.appendChild(hint);
            wrap.appendChild(lab);
            if (f.type === "checkbox") {
              const inp = document.createElement("select");
              inp.id = "cll-" + f.key;
              inp.dataset.llmKey = f.key;
              for (const opt of [
                { v: "", t: "use default" },
                { v: "true", t: "true" },
                { v: "false", t: "false" },
              ]) {
                const o = document.createElement("option");
                o.value = opt.v;
                o.textContent = opt.t;
                inp.appendChild(o);
              }
              if (lim[f.key] === true) inp.value = "true";
              else if (lim[f.key] === false) inp.value = "false";
              wrap.appendChild(inp);
            } else {
              const inp = document.createElement("input");
              inp.type = "number";
              inp.id = "cll-" + f.key;
              inp.dataset.llmKey = f.key;
              inp.min = "0";
              inp.placeholder = "use default";
              if (lim[f.key] !== null && lim[f.key] !== undefined) inp.value = String(lim[f.key]);
              wrap.appendChild(inp);
            }
            form.appendChild(wrap);
          }
        }

        function buildClientToolsCheckboxes(catalogTools, disabledSet) {
          const listEl = document.getElementById("client-tools-list");
          const loading = document.getElementById("client-tools-loading");
          loading.classList.add("hidden");
          listEl.classList.remove("hidden");
          listEl.replaceChildren();
          const disabled = disabledSet || new Set();
          let lastKind = "";
          for (const t of catalogTools) {
            if (t.kind !== lastKind) {
              lastKind = t.kind;
              const h = document.createElement("div");
              h.className = "hint";
              h.style.marginTop = "0.5rem";
              h.style.fontWeight = "600";
              h.textContent =
                t.kind === "meta"
                  ? "Meta tools"
                  : t.kind === "admin"
                    ? "Administration"
                    : "Upstream";
              listEl.appendChild(h);
            }
            const row = document.createElement("label");
            row.className = "client-tool-row";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.dataset.toolName = t.toolName;
            cb.checked = !disabled.has(t.toolName);
            row.appendChild(cb);
            const span = document.createElement("span");
            span.innerHTML =
              "<code>" +
              esc(t.toolName) +
              "</code>" +
              (t.domain
                ? ' <span class="hint">(' + esc(t.domain) + ")</span>"
                : "");
            row.appendChild(span);
            listEl.appendChild(row);
          }
        }

        function readClientLlmLimitsFromForm() {
          const out = {};
          for (const f of CLIENT_LLM_FIELDS) {
            const el = document.querySelector('[data-llm-key="' + f.key + '"]');
            if (!el) continue;
            if (f.type === "checkbox") {
              const v = el.value;
              out[f.key] = v === "" ? null : v === "true";
              continue;
            }
            const raw = el.value.trim();
            if (raw === "") out[f.key] = null;
            else {
              const n = parseInt(raw, 10);
              out[f.key] = Number.isFinite(n) ? n : null;
            }
          }
          return out;
        }

        function readDisabledToolsFromForm() {
          const disabled = [];
          document
            .querySelectorAll("#client-tools-list input[type=checkbox][data-tool-name]")
            .forEach((cb) => {
              if (!cb.checked) disabled.push(cb.dataset.toolName);
            });
          return disabled;
        }

        function formatClientConfigTitle(label, clientId) {
          const lab = (label || "").trim();
          if (lab) return lab + " (" + clientId + ")";
          return clientId;
        }

        function setClientConfigHeading(label, clientId) {
          document.getElementById("client-config-id").textContent = formatClientConfigTitle(
            label,
            clientId
          );
        }

        async function openClientConfig(clientId, labelHint) {
          clientConfigEditingId = clientId;
          const panel = document.getElementById("client-config-panel");
          const msg = document.getElementById("client-config-msg");
          msg.innerHTML = "";
          panel.classList.remove("hidden");
          setClientConfigHeading(labelHint || "", clientId);
          document.getElementById("client-tools-loading").classList.remove("hidden");
          document.getElementById("client-tools-list").classList.add("hidden");
          try {
            const [clientRes, catalog] = await Promise.all([
              apiFetch(apiClients + "/" + encodeURIComponent(clientId)),
              loadClientToolCatalog(),
            ]);
            const data = await clientRes.json().catch(() => ({}));
            if (!clientRes.ok) {
              msg.innerHTML =
                '<div class="msg err">Failed to load client (' + esc(clientRes.status) + ")</div>";
              return;
            }
            document.getElementById("client-config-label").value = data.label || "";
            setClientConfigHeading(data.label, clientId);
            document.getElementById("client-config-instructions").value = data.instructions || "";
            document.getElementById("client-config-admin").checked = !!data.can_admin;
            buildClientLlmLimitsForm(data.global_llm_limits, data.llm_limits);
            buildClientToolsCheckboxes(catalog.tools || [], new Set(data.disabled_tools || []));
            panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
          } catch (e) {
            msg.innerHTML = '<div class="msg err">' + esc(String(e)) + "</div>";
          }
        }

        async function loadClients() {
          clientMsg.innerHTML = "";
          hideClientConfigPanel();
          const res = await apiFetch(apiClients);
          if (!res.ok) {
            clientMsg.innerHTML =
              '<div class="msg err">Failed to load clients (' + esc(res.status) + ")</div>";
            return;
          }
          const list = await res.json();
          clientsBody.replaceChildren();
          if (!list.length) {
            clientsTable.classList.add("hidden");
            clientsEmpty.classList.remove("hidden");
            return;
          }
          clientsEmpty.classList.add("hidden");
          clientsTable.classList.remove("hidden");
          for (const c of list) {
            const tr = document.createElement("tr");
            const tdId = document.createElement("td");
            tdId.textContent = c.id;
            const tdL = document.createElement("td");
            tdL.textContent = c.label;
            const tdC = document.createElement("td");
            tdC.textContent = c.created_at || "—";
            const tdP = document.createElement("td");
            const parts = [];
            if (c.has_llm_overrides) parts.push("custom limits");
            if (c.disabled_tools_count) parts.push(c.disabled_tools_count + " off");
            if (c.has_instructions) parts.push("instructions");
            if (c.can_admin) parts.push("admin API");
            tdP.textContent = parts.length ? parts.join(", ") : "defaults";
            const tdA = document.createElement("td");
            const cfg = document.createElement("button");
            cfg.type = "button";
            cfg.className = "secondary small";
            cfg.textContent = "Configure";
            cfg.addEventListener("click", () => openClientConfig(c.id, c.label));
            const del = document.createElement("button");
            del.type = "button";
            del.className = "danger";
            del.textContent = "Revoke";
            del.addEventListener("click", async () => {
              if (!confirm("Revoke token for “" + c.label + "”? It stops working immediately.")) return;
              const r = await apiFetch(apiClients + "/" + encodeURIComponent(c.id), {
                method: "DELETE",
              });
              if (!r.ok) {
                alert("Revoke failed: " + r.status);
                return;
              }
              hideClientConfigPanel();
              loadClients();
            });
            tdA.appendChild(cfg);
            tdA.appendChild(del);
            tr.append(tdId, tdL, tdC, tdP, tdA);
            clientsBody.appendChild(tr);
          }
        }

        document.getElementById("client-create").addEventListener("click", async () => {
          clientMsg.innerHTML = "";
          hideClientTokenOnce();
          const label = document.getElementById("client-label").value.trim();
          if (!label) {
            clientMsg.innerHTML = '<div class="msg err">Label is required.</div>';
            return;
          }
          const res = await apiFetch(apiClients, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ label }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const d = data.detail;
            clientMsg.innerHTML =
              '<div class="msg err">' +
              esc(typeof d === "string" ? d : JSON.stringify(data)) +
              "</div>";
            return;
          }
          const pre = document.getElementById("client-token-once");
          pre.classList.remove("hidden");
          pre.textContent =
            "Save this token now — it will not be shown again:\n\n" + (data.token || "") + "\n";
          clientMsg.innerHTML = '<div class="msg ok">Client created.</div>';
          document.getElementById("client-label").value = "";
          loadClients();
        });

        document.getElementById("client-refresh").addEventListener("click", () => {
          clientToolCatalogCache = null;
          loadClients();
        });

        document.getElementById("client-config-cancel").addEventListener("click", hideClientConfigPanel);

        document.getElementById("client-config-save").addEventListener("click", async () => {
          const msg = document.getElementById("client-config-msg");
          msg.innerHTML = "";
          if (!clientConfigEditingId) return;
          const label = document.getElementById("client-config-label").value.trim();
          if (!label) {
            msg.innerHTML = '<div class="msg err">Label is required.</div>';
            return;
          }
          const instructions = document.getElementById("client-config-instructions").value;
          if (instructions.length > 12000) {
            msg.innerHTML = '<div class="msg err">Instructions must be at most 12000 characters.</div>';
            return;
          }
          const body = {
            label,
            instructions,
            llm_limits: readClientLlmLimitsFromForm(),
            disabled_tools: readDisabledToolsFromForm(),
            can_admin: document.getElementById("client-config-admin").checked,
          };
          const res = await apiFetch(apiClients + "/" + encodeURIComponent(clientConfigEditingId), {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const d = data.detail;
            msg.innerHTML =
              '<div class="msg err">' +
              esc(typeof d === "string" ? d : JSON.stringify(data)) +
              "</div>";
            return;
          }
          msg.innerHTML = '<div class="msg ok">Client settings saved.</div>';
          clientToolCatalogCache = null;
          loadClients();
          openClientConfig(clientConfigEditingId);
        });

        async function loadDomainsPanel() {
          const domainMsg = document.getElementById("domain-msg");
          domainMsg.innerHTML = "";
          const res = await apiFetch(apiDomains);
          const domainsBody = document.getElementById("domains-body");
          const domainsTable = document.getElementById("domains-table");
          const domainsEmpty = document.getElementById("domains-empty");
          if (!res.ok) {
            domainMsg.innerHTML =
              '<div class="msg err">Failed to load domains (' + esc(res.status) + ")</div>";
            return;
          }
          const list = await res.json();
          domainsBody.replaceChildren();
          if (!list.length) {
            domainsTable.classList.add("hidden");
            domainsEmpty.classList.remove("hidden");
            return;
          }
          domainsEmpty.classList.add("hidden");
          domainsTable.classList.remove("hidden");
          for (const d of list) {
            const tr = document.createElement("tr");
            const tdI = document.createElement("td");
            tdI.textContent = d.id;
            const tdL = document.createElement("td");
            tdL.textContent = d.label;
            const tdA = document.createElement("td");
            if (d.id !== "default") {
              const del = document.createElement("button");
              del.type = "button";
              del.className = "danger";
              del.textContent = "Delete";
              del.addEventListener("click", async () => {
                if (!confirm("Delete domain “" + d.id + "”? Servers using it must be reassigned first.")) return;
                const r = await apiFetch(apiDomains + "/" + encodeURIComponent(d.id), { method: "DELETE" });
                if (!r.ok) {
                  const err = await r.json().catch(() => ({}));
                  alert(typeof err.detail === "string" ? err.detail : "Delete failed: " + r.status);
                  return;
                }
                await loadDomainsList();
                loadDomainsPanel();
                loadServers();
              });
              tdA.appendChild(del);
            } else {
              tdA.textContent = "—";
            }
            tr.append(tdI, tdL, tdA);
            domainsBody.appendChild(tr);
          }
        }

        document.getElementById("domain-add").addEventListener("click", async () => {
          const domainMsg = document.getElementById("domain-msg");
          domainMsg.innerHTML = "";
          const id = document.getElementById("domain-new-id").value.trim();
          const label = document.getElementById("domain-new-label").value.trim();
          if (!id || !label) {
            domainMsg.innerHTML = '<div class="msg err">Id and label are required.</div>';
            return;
          }
          const res = await apiFetch(apiDomains, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id, label }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            domainMsg.innerHTML =
              '<div class="msg err">' +
              esc(typeof data.detail === "string" ? data.detail : JSON.stringify(data)) +
              "</div>";
            return;
          }
          domainMsg.innerHTML = '<div class="msg ok">Domain added.</div>';
          document.getElementById("domain-new-id").value = "";
          document.getElementById("domain-new-label").value = "";
          await loadDomainsList();
          loadDomainsPanel();
        });

        document.getElementById("domain-refresh").addEventListener("click", () => {
          loadDomainsList().then(() => loadDomainsPanel());
        });

        const adminLogsPre = document.getElementById("admin-logs");
        const logsEmpty = document.getElementById("logs-empty");
        const adminLlmPre = document.getElementById("admin-llm-preview");
        const llmEmpty = document.getElementById("llm-empty");

        let logsPollId = null;
        const LOGS_POLL_MS = 2500;
        const LOGS_TAIL_SLOP = 12;
        let logsFollowingTail = false;

        function stopLogsPolling() {
          if (logsPollId != null) {
            clearInterval(logsPollId);
            logsPollId = null;
          }
        }

        function startLogsPolling() {
          stopLogsPolling();
          logsPollId = setInterval(() => {
            if (!panelLogs.classList.contains("hidden")) {
              loadLogs({ silent: true });
            }
          }, LOGS_POLL_MS);
        }

        function logsScrollMax(el) {
          return Math.max(0, el.scrollHeight - el.clientHeight);
        }

        function logsNearBottom(el) {
          return el.scrollHeight - el.scrollTop - el.clientHeight <= LOGS_TAIL_SLOP;
        }

        function logsPinToBottom(el) {
          const y = logsScrollMax(el);
          el.scrollTop = y;
          requestAnimationFrame(() => {
            el.scrollTop = logsScrollMax(el);
          });
        }

        async function loadLogs(opts) {
          opts = opts || {};
          const silent = opts.silent === true;
          const forceTail = opts.forceTail === true;
          const el = adminLogsPre;
          const logsTabOpen = !panelLogs.classList.contains("hidden");

          if (!silent) {
            logsEmpty.classList.remove("hidden");
            logsEmpty.textContent = "Loading…";
            adminLogsPre.classList.add("hidden");
          }
          let lim = parseInt(document.getElementById("logs-limit").value, 10);
          if (!Number.isFinite(lim) || lim < 1) lim = 500;
          if (lim > 2000) lim = 2000;
          const res = await apiFetch(apiLogs + "?limit=" + encodeURIComponent(String(lim)));
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            if (!silent) {
              logsEmpty.textContent =
                typeof data.detail === "string" ? data.detail : "Failed to load logs (" + res.status + ").";
            }
            return;
          }
          const lines = data.lines || [];
          logsEmpty.classList.add("hidden");
          adminLogsPre.classList.remove("hidden");

          const preShown = !el.classList.contains("hidden");
          let stickToBottom;
          if (!logsTabOpen) {
            stickToBottom = true;
          } else if (!preShown || !silent) {
            stickToBottom = true;
          } else {
            stickToBottom = logsNearBottom(el);
          }
          if (forceTail) stickToBottom = true;

          const prevScrollTop = el.scrollTop;
          const prevScrollHeight = el.scrollHeight;

          adminLogsPre.textContent = lines.length ? lines.join("\n") : "(no log lines yet)";

          requestAnimationFrame(() => {
            if (!logsTabOpen) return;
            if (stickToBottom) {
              logsPinToBottom(el);
              logsFollowingTail = true;
            } else {
              const delta = el.scrollHeight - prevScrollHeight;
              el.scrollTop = Math.max(0, Math.min(prevScrollTop + delta, logsScrollMax(el)));
              logsFollowingTail = false;
            }
          });
        }

        adminLogsPre.addEventListener(
          "scroll",
          () => {
            if (!panelLogs.classList.contains("hidden") && !adminLogsPre.classList.contains("hidden")) {
              logsFollowingTail = logsNearBottom(adminLogsPre);
            }
          },
          { passive: true },
        );

        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState !== "visible") return;
          if (panelLogs.classList.contains("hidden") || adminLogsPre.classList.contains("hidden")) return;
          if (!logsFollowingTail) return;
          logsPinToBottom(adminLogsPre);
          loadLogs({ silent: true, forceTail: true });
        });

        async function loadLlmPreview() {
          llmEmpty.classList.remove("hidden");
          llmEmpty.textContent = "Loading…";
          adminLlmPre.classList.add("hidden");
          const res = await apiFetch(apiLlmPreview);
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            llmEmpty.textContent =
              typeof data.detail === "string" ? data.detail : "Failed to load preview (" + res.status + ").";
            return;
          }
          llmEmpty.classList.add("hidden");
          adminLlmPre.classList.remove("hidden");
          adminLlmPre.textContent = JSON.stringify(data, null, 2);
        }

        document.getElementById("logs-refresh").addEventListener("click", () => loadLogs({ silent: false }));
        document.getElementById("llm-refresh").addEventListener("click", () => loadLlmPreview());
        document.getElementById("live-refresh").addEventListener("click", () => loadLive({ silent: false }));

        function hideClientTokenOnce() {
          const pre = document.getElementById("client-token-once");
          pre.classList.add("hidden");
          pre.textContent = "";
        }

        document.querySelectorAll("button.tab").forEach((btn) => {
          btn.addEventListener("click", () => {
            const tab = btn.getAttribute("data-tab");
            if (tab !== "clients") {
              hideClientTokenOnce();
            }
            document.querySelectorAll("button.tab").forEach((b) => {
              const on = b.getAttribute("data-tab") === tab;
              b.classList.toggle("active", on);
              b.setAttribute("aria-selected", on ? "true" : "false");
            });
            panelReg.classList.toggle("hidden", tab !== "register");
            panelExp.classList.toggle("hidden", tab !== "explore");
            panelClients.classList.toggle("hidden", tab !== "clients");
            panelLive.classList.toggle("hidden", tab !== "live");
            panelDomains.classList.toggle("hidden", tab !== "domains");
            panelLogs.classList.toggle("hidden", tab !== "logs");
            panelLlm.classList.toggle("hidden", tab !== "llm");
            if (tab === "explore") loadServers();
            if (tab === "clients") loadClients();
            if (tab === "live") {
              loadLive({ silent: false });
              startLivePolling();
            } else {
              stopLivePolling();
            }
            if (tab === "domains") loadDomainsPanel();
            if (tab === "logs") {
              loadLogs({ silent: false });
              startLogsPolling();
            } else {
              stopLogsPolling();
            }
            if (tab === "llm") loadLlmPreview();
          });
        });

        wizGo.addEventListener("click", async () => {
          wizMsg.innerHTML = "";
          wizLog.classList.remove("hidden");
          wizLog.textContent = "Running…";
          const ecosystem = document.querySelector('input[name="wiz-ecosystem"]:checked').value;
          const server_id = document.getElementById("wiz-id").value.trim();
          const pkgSpec = document.getElementById("wiz-pkg").value.trim();
          const display_name = document.getElementById("wiz-display").value.trim() || null;
          let env;
          try {
            env = parseJsonObject(document.getElementById("wiz-env").value, "Environment");
          } catch (e) {
            wizLog.classList.add("hidden");
            wizLog.textContent = "";
            wizMsg.innerHTML =
              '<div class="msg err">' + esc(e.message || String(e)) + "</div>";
            return;
          }
          if (!server_id || !pkgSpec) {
            wizLog.classList.add("hidden");
            wizMsg.innerHTML = '<div class="msg err">Server id and package are required.</div>';
            return;
          }
          try {
            const r = await apiFetch(api + "/register-stdio-package", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                ecosystem,
                server_id,
                domain: document.getElementById("wiz-domain").value.trim() || "default",
                package: pkgSpec,
                display_name,
                llm_context: document.getElementById("wiz-llm").value.trim(),
                env,
              }),
            });
            const data = await r.json().catch(() => ({}));
            if (r.status === 403 || r.status === 400 || r.status === 409) {
              wizLog.classList.add("hidden");
              const d = data.detail;
              wizMsg.innerHTML =
                '<div class="msg err">' +
                esc(typeof d === "string" ? d : JSON.stringify(d, null, 2)) +
                "</div>";
              return;
            }
            if (!r.ok) {
              wizLog.textContent = JSON.stringify(data, null, 2);
              wizMsg.innerHTML =
                '<div class="msg err">Request failed (' + esc(r.status) + ").</div>";
              return;
            }
            let out = data.log || "";
            if (data.detail) out += "\n\n" + data.detail;
            if (data.registered) out += "\n\n— Server registered —";
            wizLog.textContent = out || JSON.stringify(data, null, 2);
            if (data.ok && data.registered) {
              wizMsg.innerHTML = '<div class="msg ok">Stdio server saved. You can inspect it in Explore.</div>';
              loadServers();
            } else {
              wizMsg.innerHTML = '<div class="msg err">Install or registration did not complete (see log).</div>';
            }
          } catch (e) {
            wizLog.textContent = String((e && e.message) || e);
            wizMsg.innerHTML = '<div class="msg err">Network or client error.</div>';
          }
        });

        httpForm.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          formMsg.innerHTML = "";
          const id = document.getElementById("f-id").value.trim();
          const display_name = document.getElementById("f-name").value.trim() || null;
          const enabled = document.getElementById("f-enabled").checked;
          let body;
          try {
            const url = document.getElementById("f-url").value.trim();
            const headers = parseJsonObject(document.getElementById("f-headers").value, "Headers");
            const http_transport = httpForm.querySelector('input[name="http_transport"]:checked').value;
            body = {
              id,
              domain: document.getElementById("f-domain").value.trim() || "default",
              type: "http",
              enabled,
              display_name,
              url,
              headers,
              http_transport,
              llm_context: document.getElementById("f-llm").value.trim(),
            };
          } catch (e) {
            formMsg.innerHTML =
              '<div class="msg err">' + esc(e.message || String(e)) + "</div>";
            return;
          }
          const res = await apiFetch(api, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const detail = data.detail;
            const msg =
              typeof detail === "string"
                ? detail
                : Array.isArray(detail)
                  ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
                  : JSON.stringify(data);
            formMsg.innerHTML = '<div class="msg err">' + esc(msg) + "</div>";
            return;
          }
          formMsg.innerHTML = '<div class="msg ok">Saved.</div>';
          httpForm.reset();
          document.getElementById("f-enabled").checked = true;
          httpForm.querySelector('input[name="http_transport"][value="streamable-http"]').checked = true;
          fillAllDomainSelects();
          loadServers();
        });

        document.getElementById("add-stdio-manual-form").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const msgEl = document.getElementById("stdio-manual-msg");
          msgEl.innerHTML = "";
          const id = document.getElementById("ms-id").value.trim().toLowerCase();
          const cmd = document.getElementById("ms-command").value.trim();
          if (!id || !cmd) {
            msgEl.innerHTML = '<div class="msg err">Server id and command are required.</div>';
            return;
          }
          let env;
          try {
            env = parseJsonObject(document.getElementById("ms-env").value, "Environment");
          } catch (e) {
            msgEl.innerHTML =
              '<div class="msg err">' + esc(e.message || String(e)) + "</div>";
            return;
          }
          const cwdRaw = document.getElementById("ms-cwd").value.trim();
          const body = {
            id,
            domain: document.getElementById("ms-domain").value.trim() || "default",
            type: "stdio",
            enabled: document.getElementById("ms-enabled").checked,
            command: cmd,
            cwd: cwdRaw || null,
            display_name: document.getElementById("ms-display").value.trim() || null,
            llm_context: document.getElementById("ms-llm").value.trim(),
            env,
            stdio_node_inspect: false,
            stdio_node_inspect_brk: false,
            stdio_node_inspect_port: 9229,
          };
          const res = await apiFetch(api, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            const detail = data.detail;
            const msg =
              typeof detail === "string"
                ? detail
                : Array.isArray(detail)
                  ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
                  : JSON.stringify(data);
            msgEl.innerHTML = '<div class="msg err">' + esc(msg) + "</div>";
            return;
          }
          msgEl.innerHTML =
            '<div class="msg ok">Registered. Open <em>Explore servers</em> to verify or edit.</div>';
          document.getElementById("add-stdio-manual-form").reset();
          document.getElementById("ms-enabled").checked = true;
          document.getElementById("ms-env").value = "{}";
          fillAllDomainSelects();
          loadServers();
        });

        loadServers();
      })();
    

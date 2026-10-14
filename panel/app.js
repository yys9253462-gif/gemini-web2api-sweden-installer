const $ = (id) => document.getElementById(id);
let csrfToken = '';
let currentConfig = null;
let toastTimer = null;
const panelBase = window.location.pathname.startsWith('/panel/') ? '/panel/' : '/';
const panelPath = (path) => panelBase + path.replace(/^\/+/, '');

function toast(message, type = 'success') {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show ${type === 'error' ? 'error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3600);
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (options.method && options.method !== 'GET' && path !== '/api/login') headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(panelPath(path), { credentials: 'same-origin', ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== '/api/login') {
    showLogin();
    throw new Error('登录会话已失效，请重新登录。');
  }
  if (!response.ok) throw new Error(data.error || data.message || `请求失败（${response.status}）`);
  return data;
}

function showLogin(message = '') {
  $('app-view').classList.add('hidden');
  $('login-view').classList.remove('hidden');
  $('login-error').textContent = message;
}

function showApp() {
  $('login-view').classList.add('hidden');
  $('app-view').classList.remove('hidden');
}

function nav(page) {
  document.querySelectorAll('.nav-item[data-page]').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  document.querySelectorAll('.page').forEach((section) => section.classList.toggle('active', section.id === `page-${page}`));
  const titles = { overview: '运行总览', account: '账号接入', settings: 'API 配置', playground: '在线测试', logs: '服务日志' };
  $('page-title').textContent = titles[page] || '运行总览';
  if (page === 'logs') loadLogs();
  if (page === 'overview') loadStatus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function loadSession() {
  try {
    const data = await api('/api/session');
    if (!data.authenticated) return showLogin();
    csrfToken = data.csrf;
    showApp();
    await Promise.all([loadStatus(), loadConfig()]);
  } catch (error) {
    showLogin(error.message);
  }
}

async function loadStatus() {
  try {
    const d = await api('/api/status');
    const cfg = d.config || {};
    $('service-status').textContent = d.service === 'active' ? '运行中' : (d.service === 'failed' ? '启动失败' : '未运行');
    $('service-orb').classList.toggle('bad', d.service !== 'active');
    $('api-health').textContent = d.api_healthy ? 'ONLINE' : 'OFFLINE';
    $('api-health').style.color = d.api_healthy ? 'var(--mint)' : 'var(--red)';
    $('default-model').textContent = cfg.default_model || '—';
    $('api-key-summary').textContent = cfg.api_key_count ? `${cfg.api_key_count} 个已配置` : '未配置';
    $('account-status').textContent = cfg.cookie_configured ? 'Cookie 已配置' : '匿名模式';
    $('sidebar-domain').textContent = d.domain || '仅本机 / SSH 隧道';
    $('api-url').textContent = d.api_url || '—';
    const configured = cfg.cookie_configured ? 1 : 0;
    $('cookie-check').textContent = configured ? '✓' : '○';
    $('cookie-check').classList.toggle('done', !!configured);
    $('cookie-check-copy').textContent = configured ? '账号 Cookie 已保存' : '添加浏览器 Cookie 以使用自己的会话';
    $('api-check').textContent = cfg.default_model ? '✓' : '○';
    $('api-check').classList.toggle('done', !!cfg.default_model);
    $('setup-count').textContent = `${configured + (cfg.default_model ? 1 : 0)} / 2`;
    $('cookie-badge').textContent = cfg.cookie_configured ? '已连接' : '未配置';
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadConfig() {
  try {
    currentConfig = await api('/api/config');
    $('model-input').value = currentConfig.default_model || '';
    $('timeout-input').value = currentConfig.request_timeout_sec ?? 180;
    $('retry-input').value = currentConfig.retry_attempts ?? 3;
    $('delay-input').value = currentConfig.retry_delay_sec ?? 2;
    $('temporary-input').checked = !!currentConfig.temporary_chats;
    $('proxy-input').value = '';
    $('proxy-input').placeholder = currentConfig.proxy_configured ? (currentConfig.proxy_preview || '已配置（地址已隐藏）') : 'http://127.0.0.1:7890';
    $('clear-proxy').checked = false;
    $('auth-user').value = currentConfig.auth_user || '';
    $('xsrf-token').value = '';
    $('key-preview').textContent = currentConfig.api_key_preview || '未配置';
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadLogs() {
  $('logs-output').textContent = '正在读取日志…';
  try {
    const data = await api(`/api/logs?lines=${encodeURIComponent($('log-lines').value)}`);
    $('logs-output').textContent = (data.lines || []).join('\n') || '暂无日志。';
  } catch (error) {
    $('logs-output').textContent = error.message;
  }
}

function busy(button, label) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = label;
  return () => { button.disabled = false; button.innerHTML = original; };
}

async function copyText(value) {
  try { await navigator.clipboard.writeText(value); toast('已复制到剪贴板。'); }
  catch { toast('复制失败，请手动选择文本。', 'error'); }
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const done = busy(button, '正在验证…');
  $('login-error').textContent = '';
  try {
    const data = await api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('login-password').value }) });
    csrfToken = data.csrf;
    $('login-password').value = '';
    showApp();
    await Promise.all([loadStatus(), loadConfig()]);
  } catch (error) { $('login-error').textContent = error.message; }
  finally { done(); }
});

document.querySelectorAll('.nav-item[data-page]').forEach((item) => item.addEventListener('click', () => nav(item.dataset.page)));
document.querySelectorAll('[data-go]').forEach((item) => item.addEventListener('click', () => nav(item.dataset.go)));
$('refresh-button').addEventListener('click', loadStatus);
$('refresh-logs').addEventListener('click', loadLogs);
$('log-lines').addEventListener('change', loadLogs);
$('copy-url').addEventListener('click', () => copyText($('api-url').textContent));

$('logout-button').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch {}
  csrfToken = '';
  showLogin('已退出管理面板。');
});

$('restart-button').addEventListener('click', async () => {
  if (!confirm('确认重启 Gemini Web2API？当前请求可能中断数秒。')) return;
  const done = busy($('restart-button'), '正在重启…');
  try { const d = await api('/api/service/restart', { method: 'POST', body: '{}' }); toast(d.message || '服务已重启。'); setTimeout(loadStatus, 1600); }
  catch (error) { toast(error.message, 'error'); }
  finally { done(); }
});

$('save-cookie').addEventListener('click', async () => {
  const cookie = $('cookie-input').value.trim();
  if (!cookie) return toast('请先粘贴新的 Gemini Cookie。', 'error');
  if (!confirm('Cookie 属于账号登录凭据。确认保存到本机并重启 API？')) return;
  const payload = { cookie, auth_user: $('auth-user').value.trim() || null };
  if ($('clear-xsrf').checked) payload.xsrf_token = '';
  else if ($('xsrf-token').value.trim()) payload.xsrf_token = $('xsrf-token').value.trim();
  const done = busy($('save-cookie'), '正在保存并重启…');
  try {
    const d = await api('/api/cookie', { method: 'POST', body: JSON.stringify(payload) });
    $('cookie-input').value = '';
    $('xsrf-token').value = '';
    $('clear-xsrf').checked = false;
    toast(d.message || 'Cookie 已保存。');
    await Promise.all([loadStatus(), loadConfig()]);
  } catch (error) { toast(error.message, 'error'); }
  finally { done(); }
});

$('save-settings').addEventListener('click', async () => {
  const payload = {
    default_model: $('model-input').value.trim(),
    request_timeout_sec: Number($('timeout-input').value),
    retry_attempts: Number($('retry-input').value),
    retry_delay_sec: Number($('delay-input').value),
    temporary_chats: $('temporary-input').checked,
  };
  if ($('clear-proxy').checked) payload.proxy = '';
  else if ($('proxy-input').value.trim()) payload.proxy = $('proxy-input').value.trim();
  const done = busy($('save-settings'), '正在保存并重启…');
  try { const d = await api('/api/config', { method: 'POST', body: JSON.stringify(payload) }); toast(d.message || '设置已保存。'); await Promise.all([loadStatus(), loadConfig()]); }
  catch (error) { toast(error.message, 'error'); }
  finally { done(); }
});

$('rotate-key').addEventListener('click', async () => {
  if (!confirm('轮换会立即使所有旧 API Key 失效。确认继续？')) return;
  const done = busy($('rotate-key'), '正在轮换并重启…');
  try {
    const d = await api('/api/api-key/rotate', { method: 'POST', body: '{}' });
    $('new-key-value').textContent = d.api_key;
    $('new-key-box').classList.remove('hidden');
    toast('新 API Key 已生成。请立即复制保存。');
    await Promise.all([loadStatus(), loadConfig()]);
  } catch (error) { toast(error.message, 'error'); }
  finally { done(); }
});
$('copy-key').addEventListener('click', () => copyText($('new-key-value').textContent));

$('run-test').addEventListener('click', async () => {
  const done = busy($('run-test'), '请求发送中…');
  $('test-result').classList.remove('hidden');
  $('test-result-model').textContent = '';
  $('test-result-content').textContent = '正在等待 Gemini 上游响应…';
  try {
    const d = await api('/api/test', { method: 'POST', body: JSON.stringify({ prompt: $('test-prompt').value, model: $('test-model').value.trim() }) });
    $('test-result-model').textContent = d.model || '';
    $('test-result-content').textContent = d.content || '上游返回空内容。';
  } catch (error) { $('test-result-content').textContent = error.message; }
  finally { done(); }
});

$('detect-model-button').addEventListener('click', async () => {
  if (!confirm('这会向 Google 发送一条短测试请求并消耗一次上游额度。继续？')) return;
  const done = busy($('detect-model-button'), '检测中…');
  $('model-detection-result').classList.remove('hidden');
  $('model-detection-status').textContent = '正在检测…';
  $('model-detection-http').textContent = '';
  $('model-detection-detail').textContent = '正在检查当前默认模型的 Google 上游路由。';
  try {
    const d = await api('/api/model-detection', { method: 'POST', body: '{}' });
    $('model-detection-status').textContent = d.ok ? '请求路由成功' : '请求路由失败';
    $('model-detection-http').textContent = d.route_status ? `HTTP ${d.route_status}` : '';
    $('model-detection-detail').textContent = [
      `配置模型：${d.configured_model || '—'}`,
      `Gemini Web 路由：${d.route_mode_name || '未识别'}${d.route_mode == null ? '' : `（mode=${d.route_mode}）`}`,
      `生成探针：${d.probe_matched ? '通过' : '未通过'}`,
      `网关 model 回显：${d.adapter_model_echo || '无'}（这是适配器回显，不是 Google 的型号证明）`,
      `Google 实际模型 ID：${d.identity_verified ? '已确认' : (d.identity_status || '未确认')}`,
      d.detail || '',
    ].filter(Boolean).join('\n');
  } catch (error) {
    $('model-detection-status').textContent = '检测失败';
    $('model-detection-detail').textContent = error.message;
  } finally { done(); }
});

$('password-button').addEventListener('click', () => $('password-dialog').showModal());
$('save-panel-password').addEventListener('click', async () => {
  const password = $('new-panel-password').value;
  if (password.length < 14) return toast('密码至少需要 14 位。', 'error');
  try {
    const d = await api('/api/password', { method: 'POST', body: JSON.stringify({ password }) });
    $('password-dialog').close();
    $('new-panel-password').value = '';
    toast(d.message || '面板密码已更新。');
  } catch (error) { toast(error.message, 'error'); }
});

loadSession();

(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  async function request(url, options) {
    var response = await fetch(url, Object.assign(
      { headers: { 'Content-Type': 'application/json' } },
      options || {}
    ));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || data.code || ('HTTP ' + response.status));
    return data;
  }

  function voucherCard(voucher) {
    var first = voucher.payments && voucher.payments[0];
    var reference = first ? first.reconciliation_no : '无列出的款';
    var link = voucher.download_url
      ? '<a href="' + esc(voucher.download_url) + '" target="_blank" rel="noopener">打开/复制旧链接</a>'
      : '';
    return '<div class="agent-voucher-card">' +
      '<div class="ledger-row"><span>代理</span><strong>' + esc(voucher.agent_id) + '</strong></div>' +
      '<div class="ledger-row"><span>列出款数</span><strong>' + esc(voucher.count) + '</strong></div>' +
      '<div class="ledger-row"><span>合计（分）</span><strong>' + esc(voucher.total_cents) + '</strong></div>' +
      '<div class="ledger-row"><span>首笔核对号</span><code>' + esc(reference) + '</code></div>' +
      '<div class="agent-link">' + link + '</div>' +
      '</div>';
  }

  function renderDelivery(data) {
    var vouchers = data.vouchers || (data.agent_id ? [data] : []);
    $('agent-delivery-result').innerHTML =
      '<p class="hint">已生成 ' + vouchers.length + ' 份凭证；未归属或未签发的款已过滤。</p>' +
      vouchers.map(voucherCard).join('');
  }

  async function issue() {
    var result = $('agent-delivery-result');
    try {
      var payments = JSON.parse($('delivery-payments').value || '[]');
      var agentId = $('delivery-agent').value.trim();
      var batchId = $('delivery-batch').value.trim();
      var payload = { payments: payments };
      if (batchId) payload.batch_id = batchId;
      if (agentId) payload.agent_id = agentId;
      var url = agentId ? '/api/agents/' + encodeURIComponent(agentId) + '/vouchers' : '/api/agent-vouchers';
      result.textContent = '生成中…';
      renderDelivery(await request(url, { method: 'POST', body: JSON.stringify(payload) }));
    } catch (error) {
      result.innerHTML = '<p class="verdict" data-v="REJECT">' + esc(error.message) + '</p>';
    }
  }

  async function revoke() {
    var result = $('agent-delivery-result');
    var agentId = $('delivery-agent').value.trim();
    if (!agentId) {
      result.innerHTML = '<p class="verdict" data-v="REVIEW">请先填写要撤回的代理编号。</p>';
      return;
    }
    try {
      await request('/api/agents/' + encodeURIComponent(agentId) + '/revoke', { method: 'POST' });
      result.innerHTML = '<p class="verdict" data-v="REJECT">代理 ' + esc(agentId) + ' 权限已撤回，此前链接现在会返回 403。</p>';
    } catch (error) {
      result.innerHTML = '<p class="verdict" data-v="REJECT">' + esc(error.message) + '</p>';
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!$('agent-issue')) return;
    $('agent-issue').addEventListener('click', issue);
    $('agent-revoke').addEventListener('click', revoke);
  });
})();

/* Operations only. No financial decisions are made in the browser. */
(() => {
  const csrf = document.querySelector('script[data-csrf]').dataset.csrf;
  const result = document.querySelector('#result');
  const notice = document.querySelector('#notice');
  let clearTimer;
  function show(value, sensitive = false) {
    clearTimeout(clearTimer);
    result.textContent = JSON.stringify(value, null, 2);
    if (sensitive) clearTimer = setTimeout(() => { result.textContent = ''; }, 60000);
  }
  async function refresh() {
    const response = await fetch('/admin/ops/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Sign in with an individual staff account to load operations.');
    const state = await response.json();
    const sources = {...state, clean_documents: state.documents.filter(d => d.scan_status === 'clean'),
      approved_billers: state.billers.filter(b => b.status === 'approved')};
    for (const kind of ['biller', 'debt', 'balance', 'funding', 'submission', 'settlement', 'posting', 'return', 'non_execution']) {
      sources[kind + '_evidence'] = state.evidence.filter(e => e.kind === kind);
    }
    for (const select of document.querySelectorAll('select[data-source]')) {
      const selected = select.value;
      select.replaceChildren(new Option('Choose a record', ''));
      for (const row of sources[select.dataset.source] || []) {
        const name = [row.legal_name || row.biller_name || row.kind || row.purpose,
          row.status || row.scan_status, row.amount_cents, row.id].filter(v => v !== undefined && v !== null).join(' · ');
        select.add(new Option(name, row.id));
      }
      select.value = selected;
    }
    const tables = document.querySelector('#tables');
    tables.replaceChildren();
    for (const [name, rows] of Object.entries(state)) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `${name} (${rows.length})`;
      details.append(summary);
      if (rows.length) {
        const wrap = document.createElement('div'); wrap.className = 'table-wrap';
        const table = document.createElement('table');
        const head = document.createElement('tr');
        for (const key of Object.keys(rows[0])) {
          const th = document.createElement('th'); th.textContent = key.replaceAll('_', ' '); head.append(th);
        }
        table.append(head);
        for (const row of rows) {
          const tr = document.createElement('tr');
          for (const [key, value] of Object.entries(row)) {
            const td = document.createElement('td');
            if (name === 'bills' && key === 'verification_paused' && value) {
              const link = document.createElement('a');
              link.href = '/admin/ops/bills/' + encodeURIComponent(row.id) + '/balance-reports';
              link.textContent = 'Paused — review private balance reports';
              td.append(link);
            } else td.textContent = value === null ? '—' : String(value);
            tr.append(td);
          }
          table.append(tr);
        }
        wrap.append(table); details.append(wrap);
      }
      tables.append(details);
    }
    notice.textContent = 'Records refreshed. Amounts are in cents. Document scanning requires the worker.';
  }
  for (const button of document.querySelectorAll('[data-observed-now]')) {
    button.addEventListener('click', () => {
      const now = new Date();
      button.form.querySelector('[name="observed_at"]').value = new Date(now - now.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    });
  }
  for (const form of document.querySelectorAll('form[data-endpoint], form[data-upload]')) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button:not([type="button"])'); button.disabled = true;
      notice.textContent = 'Saving…';
      try {
        let url = '/admin/ops' + (form.dataset.endpoint || '/documents');
        let body; const headers = {'X-CSRF-Token': csrf};
        if (form.hasAttribute('data-upload')) {
          body = new FormData(form);
        } else {
          const values = {};
          for (const field of form.querySelectorAll('input[name], select[name], textarea[name]')) {
            if (field.type === 'checkbox') values[field.name] = field.checked;
            else if (field.type === 'number') { if (field.value !== '') values[field.name] = Number(field.value); }
            else if (field.type === 'datetime-local') values[field.name] = new Date(field.value).toISOString();
            else values[field.name] = field.value;
          }
          url = url.replace(/\{([^}]+)\}/g, (_, name) => { const value = values[name]; delete values[name]; return encodeURIComponent(value); });
          headers['Content-Type'] = 'application/json'; body = JSON.stringify(values);
        }
        const response = await fetch(url, {method: 'POST', headers, body});
        const value = await response.json();
        show(value, form.hasAttribute('data-sensitive'));
        if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'The operation was rejected. Review the details above.');
        await refresh(); notice.textContent = 'Saved. Review the result and updated records.';
      } catch (error) { notice.textContent = error.message; }
      finally { button.disabled = false; }
    });
  }
  document.querySelector('#clear-result').addEventListener('click', () => { result.textContent = ''; });
  document.querySelector('#refresh').addEventListener('click', () => refresh().catch(e => {notice.textContent = e.message;}));
  refresh().catch(error => {notice.textContent = error.message;});
})();

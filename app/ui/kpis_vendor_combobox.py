"""Company-scoped KPIS candidate input; no networking in the browser component."""
import streamlit as st
import hashlib


HTML = """
<div class="vendor-combo">
  <label for="vendor-term">거래처명 또는 ERP 거래처코드</label>
  <input id="vendor-term" role="combobox" aria-autocomplete="list"
    aria-controls="vendor-options" aria-expanded="false" autocomplete="off" maxlength="100">
  <div id="vendor-options" role="listbox" aria-label="ERP 거래처 선택" hidden></div>
  <div class="status" role="status" aria-live="polite"></div>
</div>
"""
CSS = """
.vendor-combo {font-family:var(--st-font); color:var(--st-text-color); width:100%;}
label {display:block; font-size:14px; margin-bottom:8px;}
input {box-sizing:border-box; width:100%; min-height:40px; padding:8px 12px;
  font:inherit; color:inherit; background:var(--st-secondary-background-color,#f0f2f6);
  border:1px solid transparent; border-radius:6px;}
input:focus {outline:2px solid var(--st-primary-color,#ff4b4b); outline-offset:-2px;}
[role=listbox] {max-height:240px; overflow-y:auto; border:1px solid #a0a0a040;
  border-radius:0 0 6px 6px; background:var(--st-background-color,white);}
[role=option] {box-sizing:border-box; display:block; width:100%; text-align:left;
  border:0; background:transparent; color:inherit; font:inherit; padding:9px 12px;
  overflow-wrap:anywhere; cursor:pointer;}
[role=option]:hover, [role=option][aria-selected=true] {background:var(--st-secondary-background-color,#f0f2f6);}
.status {font-size:13px; padding-top:4px; overflow-wrap:anywhere;}
"""
JS = r"""
export default function({parentElement, data, setTriggerValue}) {
  const input = parentElement.querySelector('input');
  const list = parentElement.querySelector('[role=listbox]');
  const status = parentElement.querySelector('.status');
  const s = input._vendorState ||= {term:data.term, busy:null, seq:data.ack, changed:0,
    composing:false, open:false, active:-1, timer:null, selected:data.selected, dirty:false};
  clearTimeout(s.timer);
  if (s.busy && data.ack === s.busy) s.busy = null;
  const ready = !s.busy && s.term.trim() === data.term;
  if (ready && !data.dirty) s.dirty = false;
  if (!input._initialized) {input.value = data.selectedLabel || data.term; input._initialized = true;}
  if (ready && s.selected !== data.selected) {
    input.value = data.selectedLabel || s.term;
    s.selected = data.selected;
  }
  const close = () => {list.hidden = true; input.setAttribute('aria-expanded','false');};
  const choose = (i) => {
    if (!ready || s.busy || s.term.trim() !== data.term) return;
    s.open = false; close(); input.value = data.options[i];
    s.busy = ++s.seq;
    setTriggerValue('event', {id:s.busy, kind:'select', term:data.term, index:i});
  };
  const paint = () => {
    list.replaceChildren();
    if (!ready || s.busy || s.term.trim() !== data.term || !s.open || !data.options.length) {close(); return;}
    data.options.forEach((label,i) => {
      const option = document.createElement('button');
      option.type = 'button'; option.role = 'option'; option.id = 'vendor-option-'+i;
      option.tabIndex = -1; option.textContent = label;
      option.setAttribute('aria-selected',String(i === s.active));
      option.onmousedown = e => e.preventDefault();
      option.onclick = () => choose(i);
      list.appendChild(option);
    });
    list.hidden = false; input.setAttribute('aria-expanded','true');
    if (s.active >= 0) input.setAttribute('aria-activedescendant','vendor-option-'+s.active);
    else input.removeAttribute('aria-activedescendant');
  };
  const schedule = () => {
    clearTimeout(s.timer);
    if (s.busy || s.composing || s.term.trim() === data.term) return;
    s.timer = setTimeout(() => {
      if (!input.isConnected || s.busy || s.composing) return;
      s.busy = ++s.seq;
      status.textContent = s.term.trim() ? '검색 중...' : '';
      setTriggerValue('event', {id:s.busy, kind:'search', term:s.term.trim()});
    }, Math.max(0, 400 - (Date.now() - s.changed)));
  };
  input.oninput = (event) => {
    if (event?.isComposing) s.composing = true;
    s.term = input.value; s.changed = Date.now(); s.active = -1; s.open = true;
    close(); status.textContent = '';
    // Invalidate selection immediately, but debounce the database search separately.
    if (!s.dirty && !s.busy && s.term.trim() !== data.term) {
      s.dirty = true; s.busy = ++s.seq;
      setTriggerValue('event', {id:s.busy, kind:'invalidate', term:s.term.trim()});
    }
    schedule();
  };
  const compositionStart = () => {s.composing = true; clearTimeout(s.timer);};
  const compositionEnd = () => {s.composing = false; input.oninput();};
  input.addEventListener('compositionstart', compositionStart);
  input.addEventListener('compositionend', compositionEnd);
  input.onfocus = () => {s.open = true; paint();};
  input.onblur = () => {s.open = false; close();};
  input.onkeydown = e => {
    if (s.composing || e.isComposing) return;
    if (e.key === 'Escape') {s.open = false; close();}
    if (ready && data.options.length && ['ArrowDown','ArrowUp'].includes(e.key)) {
      e.preventDefault(); s.open = true;
      s.active = (s.active + (e.key === 'ArrowDown' ? 1 : -1) + data.options.length) % data.options.length;
      paint(); list.children[s.active]?.scrollIntoView({block:'nearest'});
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (s.open && s.active >= 0) choose(s.active);
    }
  };
  status.textContent = s.busy ? '검색 중...' : ready ? data.message : '';
  paint(); schedule();
  return () => {
    clearTimeout(s.timer);
    input.removeEventListener('compositionstart', compositionStart);
    input.removeEventListener('compositionend', compositionEnd);
  };
}
"""


def render_vendor_combobox(key, data, on_event):
    component = st.components.v2.component("ssai_kpis_vendor_combobox", html=HTML, css=CSS, js=JS)
    # V2 reserves '__' in widget IDs. Scope still includes company/user/epoch/service.
    widget_key = "kpis-vendor-" + hashlib.sha256(key.encode()).hexdigest()
    return component(key=widget_key, data=data,
        on_event_change=lambda: on_event(st.session_state[widget_key].get("event")))

/* Minimal, XSS-safe markdown renderer for LLM output.
   HTML is escaped FIRST; only tags this renderer itself emits reach the DOM.
   Covers what the models actually produce: #-###### headings, **bold**,
   *italic*, `inline code`, ``` fenced blocks, -/* bullets, 1. numbered lists,
   --- rules, and blank-line paragraphs. */
(function () {
  'use strict';

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function inline(s) {
    return s
      .replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  }

  window.renderMarkdown = function (src) {
    var lines = esc(src).split('\n');
    var out = [], list = null, para = [], inCode = false, code = [];

    function flushPara() {
      if (para.length) { out.push('<p>' + inline(para.join('<br>')) + '</p>'); para = []; }
    }
    function flushList() {
      if (list) { out.push('<' + list.tag + ' class="md-list">' + list.items.join('') + '</' + list.tag + '>'); list = null; }
    }

    for (var idx = 0; idx < lines.length; idx++) {
      var line = lines[idx];

      if (/^```/.test(line.trim())) {
        if (inCode) { out.push('<pre class="md-pre">' + code.join('\n') + '</pre>'); code = []; inCode = false; }
        else { flushPara(); flushList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }

      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flushPara(); flushList(); out.push('<div class="md-h md-h' + h[1].length + '">' + inline(h[2]) + '</div>'); continue; }

      if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flushPara(); flushList(); out.push('<hr class="md-hr">'); continue; }

      var ul = line.match(/^\s*[-*]\s+(.*)$/);
      var ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        var tag = ul ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
        list.items.push('<li>' + inline((ul || ol)[1]) + '</li>');
        continue;
      }

      if (!line.trim()) { flushPara(); flushList(); continue; }
      flushList();
      para.push(line.trim());
    }
    if (inCode && code.length) { out.push('<pre class="md-pre">' + code.join('\n') + '</pre>'); }
    flushPara(); flushList();
    return out.join('');
  };
})();

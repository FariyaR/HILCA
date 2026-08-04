/* Thin EventSource wrapper for the dialectic stream.
   Dispatches every named SSE event to handlers; closes cleanly on done/error
   so the browser does not auto-reconnect after the run finishes.

   Connection blips (mobile screen lock, network drop) are NOT terminal: the
   server keeps the engine running and every event carries an SSE id, so the
   browser's native auto-reconnect sends Last-Event-ID and replays only what
   was missed. A FATAL close (proxy 502/504, server restart — EventSource
   silently stops retrying those) is bridged by recreating the connection
   with ?since=<last seen seq>, so nothing is duplicated or lost. Handlers
   'reconnecting'/'reconnected' surface the state; only a sustained outage
   (MAX_FAILURES consecutive attempts) gives up and calls 'connection_lost'
   so the app can offer a checkpoint resume.
   opts.resume reconnects an interrupted run from its last checkpoint. */
(function () {
  var MAX_FAILURES = 8;
  var EVENTS = ['log', 'context_ready', 'round_started', 'agent_spawned', 'awaiting_approval',
    'roster_approved', 'ccu_message', 'agent_message', 'agent_verdict', 'convergence',
    'synthesis', 'deliverable', 'log_summary', 'human_feedback', 'paused', 'resumed',
    'resumed_from_checkpoint', 'context_usage', 'compacted'];

  window.openDialecticStream = function (runId, handlers, opts) {
    opts = opts || {};
    var es = null;
    var finished = false;
    var failures = 0;
    var lastSeq = 0;
    var retryTimer = null;

    function call(name, data) { if (handlers[name]) handlers[name](data); }

    function track(e) {
      var n = parseInt(e.lastEventId, 10);
      if (n > lastSeq) lastSeq = n;
    }

    function url() {
      var qs = [];
      if (opts.resume) qs.push('resume=1');
      if (lastSeq > 0) qs.push('since=' + lastSeq);
      return '/api/runs/' + runId + '/stream' + (qs.length ? '?' + qs.join('&') : '');
    }

    function connect() {
      es = new EventSource(url());

      es.onopen = function () {
        if (failures > 0) call('reconnected', {});
        failures = 0;
      };

      EVENTS.forEach(function (ev) {
        es.addEventListener(ev, function (e) { track(e); call(ev, JSON.parse(e.data)); });
      });

      es.addEventListener('done', function (e) {
        track(e);
        call('done', JSON.parse(e.data));
        finished = true; es.close();
      });

      // Fires for BOTH the server's named "error" event (has data) and native
      // connection errors (no data). Named errors are terminal; native errors
      // ride the browser's auto-reconnect (Last-Event-ID replay), or our own
      // recreate-with-?since= when the browser marked the stream CLOSED.
      es.addEventListener('error', function (e) {
        if (e.data) { call('error', JSON.parse(e.data)); finished = true; es.close(); return; }
        if (finished) { es.close(); return; }
        failures += 1;
        if (failures === 1) call('reconnecting', {});
        if (failures >= MAX_FAILURES) {
          finished = true; es.close();
          call('connection_lost', {
            message: 'Connection to the run was lost and could not be re-established. ' +
                     'The dialectic continues on the server and checkpoints every round — ' +
                     'you can reattach or resume from the last checkpoint.'
          });
          return;
        }
        if (es.readyState === EventSource.CLOSED) {
          var dead = es;
          retryTimer = setTimeout(function () {
            if (!finished && es === dead) connect();
          }, Math.min(1000 * failures, 8000));
        }
      });
    }

    connect();
    return {
      close: function () {
        finished = true;
        if (retryTimer) clearTimeout(retryTimer);
        if (es) es.close();
      }
    };
  };
})();

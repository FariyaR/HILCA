/* Thin EventSource wrapper for the dialectic stream.
   Dispatches every named SSE event to handlers; closes cleanly on done/error
   so the browser does not auto-reconnect after the run finishes.
   opts.resume reconnects an interrupted run from its last checkpoint. */
(function () {
  window.openDialecticStream = function (runId, handlers, opts) {
    opts = opts || {};
    var url = '/api/runs/' + runId + '/stream' + (opts.resume ? '?resume=1' : '');
    var es = new EventSource(url);
    var finished = false;

    function call(name, data) { if (handlers[name]) handlers[name](data); }

    ['log', 'context_ready', 'round_started', 'agent_spawned', 'awaiting_approval',
     'roster_approved', 'ccu_message', 'agent_message', 'agent_verdict', 'convergence',
     'synthesis', 'deliverable', 'log_summary', 'human_feedback', 'paused', 'resumed',
     'resumed_from_checkpoint', 'context_usage', 'compacted'].forEach(function (ev) {
      es.addEventListener(ev, function (e) { call(ev, JSON.parse(e.data)); });
    });

    es.addEventListener('done', function (e) {
      call('done', JSON.parse(e.data));
      finished = true; es.close();
    });

    // Fires for BOTH the server's named "error" event (has data) and native
    // connection errors (no data). A mid-run connection loss is terminal for
    // this page (the run continues server-side and checkpoints each round) —
    // close instead of letting EventSource retry, and let the app offer a
    // checkpoint resume.
    es.addEventListener('error', function (e) {
      if (e.data) { call('error', JSON.parse(e.data)); finished = true; es.close(); }
      else if (finished) { es.close(); }
      else {
        finished = true; es.close();
        call('connection_lost', {
          message: 'Connection to the run was lost. The dialectic continues on the ' +
                   'server and checkpoints every round — you can resume from the last checkpoint.'
        });
      }
    });

    return es;
  };
})();

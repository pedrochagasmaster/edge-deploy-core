"""The console page: one self-contained HTML/CSS/JS document.

Inlined on purpose — posture means the internet is never guaranteed, so the
page must render with no CDN, bundler, or font fetch. ``__CONSOLE_TOKEN__`` is
substituted per process by the server; it is what proves a request came from
this page and not from some other tab.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<link rel="icon" href="data:,">
<title>edge-deploy · release console</title>
<style>
:root{
  --void:#141a21;
  --panel:#1a222b;
  --sunk:#111820;
  --line:#2a3440;
  --ink:#dde4ec;
  --dim:#8b96a3;
  /* Commands, paths and timestamps live at --faint; it has to clear WCAG AA
     small-text contrast on --void, not just look quiet. */
  --faint:#7c8794;
  --gh:#7fb4e0;        /* github write (firewall-off) */
  --gh-band:#152535;
  --bb:#e8933f;        /* bitbucket vpn */
  --bb-band:#291d10;
  --edge:#58c0a8;      /* edge vpn */
  --edge-band:#12251f;
  --pass:#79c98f;
  --fail:#e2685c;
  --pend:#77828f;
  --warn:#d8a35a;
  --go:#7ee0a5;
  --hazard-a:#43371f;
  --hazard-b:#1b1712;
  --mono:"Cascadia Code","Cascadia Mono",Consolas,"SF Mono",ui-monospace,monospace;
  --sans:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--void);color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.45}
a{color:var(--gh)}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px 64px}

/* ---------- header / posture panel ---------- */
header{border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:30}
.masthead{max-width:1120px;margin:0 auto;padding:16px 20px 10px;display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end;justify-content:space-between}
.wordmark{font-family:var(--mono);font-size:17px;letter-spacing:.22em;font-weight:600}
.wordmark small{display:block;letter-spacing:.34em;font-size:10px;color:var(--dim);font-weight:400;margin-top:3px;text-transform:uppercase}
.posture{display:flex;gap:22px;align-items:flex-start;flex-wrap:wrap}
.pgroup{min-width:120px}
.pgroup h3{margin:0 0 5px;font-size:10px;letter-spacing:.18em;text-transform:uppercase;font-weight:600}
.pgroup.gh h3{color:var(--gh)}
.pgroup.bb h3{color:var(--bb)}
.pgroup.edge h3{color:var(--edge)}
.endpoint{font-family:var(--mono);font-size:11px;color:var(--dim);display:flex;gap:7px;align-items:center;padding:1px 0}
.endpoint .dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--faint)}
.endpoint.up .dot,.endpoint.ok .dot{background:var(--pass);box-shadow:0 0 5px rgba(121,201,143,.7)}
.endpoint.down .dot,.endpoint.fail .dot{background:transparent;border:1.5px solid var(--fail)}
.endpoint.unknown .dot{background:transparent;border:1.5px solid var(--warn)}
.endpoint.up,.endpoint.ok{color:var(--ink)}
.pstrip{max-width:1120px;margin:0 auto;padding:0 20px 9px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pchip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;border:1px solid var(--line);border-radius:4px;padding:2.5px 9px;color:var(--faint)}
.pchip.on{color:var(--ink);border-color:var(--pass);box-shadow:inset 0 0 0 1px var(--pass)}
.pchip.maybe{color:var(--dim);border-style:dashed;border-color:var(--dim)}
.pnote{max-width:1120px;margin:0 auto;padding:0 20px 11px;font-size:11.5px;color:var(--dim)}
.pnote b{color:var(--ink);font-weight:600}

/* ---------- banners ---------- */
.banner{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px;padding:9px 14px;border-radius:7px;font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--dim)}
.banner[hidden]{display:none}  /* a class-level display: wins over [hidden] */
.banner b{color:var(--ink)}
.banner.demo{border-color:var(--warn);color:var(--warn)}
.banner.readonly{border-color:var(--gh);color:var(--gh)}
.banner.offline{border-color:var(--fail);color:var(--fail)}
.rootline{font-family:var(--mono);font-size:11px;color:var(--faint);padding:14px 0 0}

/* ---------- section headings ---------- */
.sechead{display:flex;gap:10px;align-items:baseline;margin:26px 0 2px}
.sechead h2{margin:0;font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:var(--dim);font-weight:600}
.sechead .sub{font-size:11.5px;color:var(--faint)}

/* ---------- run cards ---------- */
.run{border:1px solid var(--line);border-radius:9px;background:var(--panel);margin-top:14px;overflow:hidden}
.run.spot{border-color:var(--pass);box-shadow:0 0 0 1px rgba(121,201,143,.18),0 10px 34px -20px rgba(0,0,0,.9)}
.run.closed{opacity:.66}
.run.closed:hover{opacity:1}
.runhead{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;padding:13px 16px 11px;border-bottom:1px solid var(--line)}
.runid{font-family:var(--mono);font-size:14px;font-weight:600}
.chip{font-size:10px;letter-spacing:.14em;text-transform:uppercase;border-radius:3px;padding:1.5px 7px;border:1px solid var(--line);color:var(--dim)}
.chip.tool{color:var(--ink);border-color:var(--faint)}
.chip.open{color:var(--pass);border-color:var(--pass)}
.chip.complete{color:var(--gh);border-color:var(--gh)}
.chip.abandoned{color:var(--fail);border-color:var(--fail)}
.chip.lock{color:var(--warn);border-color:var(--warn)}
.chip.training{color:var(--warn);border-color:var(--warn);letter-spacing:.18em}
.runmeta{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}

/* ---------- the posture rail (signature) ---------- */
.rail-wrap.simulated{border-top:1px solid var(--line)}
.rail-sim-tag{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--warn);padding:7px 16px;border-bottom:1px dashed var(--warn);background:var(--panel)}
.rail{display:flex;align-items:stretch;min-height:96px}
.rail.simulated{opacity:.9}
.station{flex:1 1 0;padding:12px 12px 14px;position:relative}
.station[data-req="any"]{background:var(--panel)}
.station[data-req="bb"]{background:var(--bb-band)}
.station[data-req="both"]{background:linear-gradient(90deg,var(--bb-band) 0 55%,var(--edge-band) 100%)}
.station[data-req="gh"]{background:var(--gh-band)}
.station .posture-tag{font-size:9px;letter-spacing:.16em;text-transform:uppercase;font-weight:600;color:var(--faint)}
.station[data-req="bb"] .posture-tag,.station[data-req="both"] .posture-tag{color:var(--bb)}
.station[data-req="gh"] .posture-tag{color:var(--gh)}
.posture-tag .cap-edge{color:var(--edge)}
.station h4{margin:3px 0 8px;font-family:var(--mono);font-size:12.5px;font-weight:600;letter-spacing:.02em}
.station.next h4::after{content:"◀\00a0next";color:var(--pass);font-size:10px;margin-left:8px;letter-spacing:.08em;white-space:nowrap}
.station.live{outline:1px solid var(--edge);outline-offset:-1px}
.station.live h4::after{content:"◀\00a0running";color:var(--edge);font-size:10px;margin-left:8px;letter-spacing:.08em;white-space:nowrap}
.state{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;white-space:nowrap}
.when{display:block;font-family:var(--mono);font-size:10px;color:var(--faint);margin:3px 0 0 14px}
.state .dot{width:8px;height:8px;border-radius:50%;flex:none}
.state.passed{color:var(--pass)}.state.passed .dot{background:var(--pass)}
.state.failed{color:var(--fail)}.state.failed .dot{background:var(--fail)}
.state.pending{color:var(--pend)}.state.pending .dot{background:transparent;border:1.5px solid var(--pend)}
.state.skipped{color:var(--faint)}.state.skipped .dot{background:transparent;border:1.5px dashed var(--faint)}
.nodes{margin-top:2px;display:grid;gap:3px}
.sep{flex:0 0 24px;position:relative;border-left:2px dashed var(--line)}
.sep span{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(90deg);white-space:nowrap;font-size:8.5px;letter-spacing:.22em;text-transform:uppercase;color:var(--faint);font-weight:600}
.sep.hot{border-left-color:var(--bb)}
.sep.hot span{color:var(--bb)}
.gate{flex:0 0 30px;background:repeating-linear-gradient(135deg,var(--hazard-a) 0 7px,var(--hazard-b) 7px 14px);position:relative;border-left:1px solid var(--line);border-right:1px solid var(--line)}
.gate span{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(90deg);white-space:nowrap;font-size:8.5px;letter-spacing:.3em;text-transform:uppercase;color:#a08a58;font-weight:600}
.gate.hot{outline:2px solid var(--bb);outline-offset:-2px;animation:gatepulse 1.6s ease-in-out infinite}
.gate.hot span{color:var(--bb)}
@keyframes gatepulse{0%,100%{outline-color:var(--bb)}50%{outline-color:transparent}}
@media (prefers-reduced-motion: reduce){.gate.hot{animation:none}}

/* ---------- live operation ---------- */
.activeop{display:flex;gap:10px;align-items:center;padding:9px 16px;border-top:1px solid var(--line);flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--dim)}
.activeop .op-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--edge);animation:oppulse 1.6s ease-in-out infinite}
.activeop.waiting .op-dot{background:var(--warn)}
.activeop.stalled .op-dot{background:var(--fail);animation:none}
.activeop .op-label{color:var(--ink)}
.activeop.waiting .op-note{color:var(--warn);letter-spacing:.1em;text-transform:uppercase;font-size:10px;font-weight:600}
.activeop.stalled .op-note{color:var(--fail)}
.activeop .op-when{margin-left:auto;color:var(--faint)}
@keyframes oppulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion: reduce){
  .activeop .op-dot,.term .tdot{animation:none}
  .transfer-fill{transition:none}
}
.transfer{display:flex;gap:10px;align-items:center;padding:9px 16px;border-top:1px solid var(--line);flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--dim)}
.transfer-artifact{flex:none}
.transfer-bar{flex:1 1 160px;height:6px;background:var(--void);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.transfer-fill{height:100%;background:var(--edge);transition:width .3s linear}
.transfer-stats{flex:none;color:var(--faint)}

/* ---------- refusals the console can see coming ---------- */
.blockers{list-style:none;margin:0;padding:10px 16px 4px;border-top:1px solid var(--line);background:rgba(226,104,92,.05)}
.blockers li{display:flex;gap:9px;align-items:flex-start;padding:4px 0;font-size:12.5px;color:var(--dim)}
.blockers li::before{content:"!";color:var(--fail);font-weight:700;flex:none;width:12px;text-align:center}
/* One flex item for the whole sentence, or each run of text between <b> and
   <code> tags wraps on its own. */
.blockers li>span{flex:1 1 auto}
.blockers li b{color:var(--ink);font-weight:600}
.blockers li code{font-family:var(--mono);font-size:11px;color:var(--faint)}
.actside .blocked-why{font-size:10px;color:var(--fail);max-width:22ch;text-align:right;line-height:1.35}

/* ---------- action rows: every command is a button ---------- */
.actions{border-top:1px solid var(--line)}
.act{display:flex;gap:12px;align-items:flex-start;padding:11px 16px;border-bottom:1px solid rgba(42,52,64,.55)}
.act:last-child{border-bottom:0}
.act.primary{background:linear-gradient(90deg,rgba(126,224,165,.09),transparent 62%)}
.act.danger{background:linear-gradient(90deg,rgba(226,104,92,.07),transparent 62%)}
.actmain{flex:1 1 320px;min-width:0}
.acttext{font-size:12.5px;color:var(--dim)}
.act.primary .acttext{color:var(--ink)}
.actcmd{display:flex;gap:8px;align-items:center;margin-top:6px}
.actcmd code{font-family:var(--mono);font-size:11px;color:var(--faint);background:var(--sunk);border:1px solid var(--line);border-radius:5px;padding:4px 8px;overflow-x:auto;white-space:nowrap;scrollbar-width:thin;flex:1 1 auto}
.actside{flex:0 0 auto;display:flex;flex-direction:column;gap:5px;align-items:flex-end;padding-top:2px}
.need{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;font-weight:600;white-space:nowrap}
.need.gh{color:var(--gh)}.need.bb,.need.both{color:var(--bb)}.need.edge{color:var(--edge)}.need.any{color:var(--faint)}
.readiness{font-family:var(--mono);font-size:10px;color:var(--faint);white-space:nowrap}
.readiness.ok{color:var(--pass)}.readiness.blocked{color:var(--fail)}
button{font-family:inherit}
button.run{font-family:var(--sans);font-size:12.5px;font-weight:600;background:none;border:1px solid var(--faint);color:var(--ink);border-radius:6px;padding:7px 14px;cursor:pointer;flex:none;white-space:nowrap}
button.run:hover:not(:disabled){border-color:var(--ink);background:rgba(221,228,236,.06)}
button.run:focus-visible{outline:2px solid var(--gh);outline-offset:2px}
button.run:disabled{opacity:.4;cursor:not-allowed}
button.run.primary{border-color:var(--go);color:var(--go);background:rgba(126,224,165,.09)}
button.run.primary:hover:not(:disabled){background:rgba(126,224,165,.18)}
button.run.big{font-size:14px;padding:10px 20px}
button.run.danger{border-color:rgba(226,104,92,.6);color:var(--fail)}
button.run.danger:hover:not(:disabled){background:rgba(226,104,92,.12)}
button.copy{font-family:var(--mono);font-size:10.5px;background:none;border:1px solid var(--line);color:var(--faint);border-radius:5px;padding:4px 9px;cursor:pointer;flex:none}
button.copy:hover{color:var(--ink);border-color:var(--faint)}
button.copy:focus-visible{outline:2px solid var(--gh);outline-offset:2px}

/* ---------- decision card: should I release? ---------- */
.decision{border:1px solid var(--line);border-radius:9px;background:var(--panel);margin-top:14px;overflow:hidden}
.decision[data-suggest="1"]{border-color:var(--warn);box-shadow:0 0 0 1px rgba(216,163,90,.14)}
.dechead{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;padding:13px 16px 8px}
.toolname{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.toolroot{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:auto}
.verdict{font-size:10px;letter-spacing:.14em;text-transform:uppercase;border:1px solid var(--line);border-radius:3px;padding:1.5px 7px;color:var(--dim)}
.verdict.ok{color:var(--pass);border-color:var(--pass)}
.verdict.warn{color:var(--warn);border-color:var(--warn)}
.verdict.dim{color:var(--faint)}
.headline{margin:0;padding:0 16px 12px;font-size:16px;line-height:1.35;color:var(--ink);font-weight:600}
.headline.calm{color:var(--dim);font-weight:400;font-size:14px}
.evidence{display:flex;gap:0;flex-wrap:wrap;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--sunk)}
.ev{flex:1 1 160px;padding:10px 16px;border-right:1px solid var(--line)}
.ev:last-child{border-right:0}
.ev h5{margin:0 0 4px;font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);font-weight:600}
.ev b{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--ink)}
.ev span{display:block;font-size:11px;color:var(--dim);margin-top:2px}
.ev span.warn{color:var(--warn)}
.ev span.ok{color:var(--pass)}
.checklist{list-style:none;margin:0;padding:10px 16px 4px}
.checklist li{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:5px 0;font-size:12.5px;color:var(--dim)}
.checklist li::before{content:"○";color:var(--faint);flex:none;width:12px}
.checklist li.ok::before{content:"✓";color:var(--pass)}
.checklist li.blocked::before{content:"!";color:var(--fail);font-weight:700}
.checklist li.manual::before{content:"◈";color:var(--warn)}
.checklist li b{color:var(--ink);font-weight:600}
.checklist li .grow{flex:1 1 40px}
/* One flex item for the whole sentence: otherwise each run of text between
   <b> tags becomes its own item and the row breaks in odd places. */
.checklist .ctext{flex:1 1 280px}
/* Button, command and posture belong together: let the group wrap as a unit
   rather than stranding a lone readiness marker on the next line. */
.checkact{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.cta{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px 14px;border-top:1px solid var(--line)}
.cta code{font-family:var(--mono);font-size:11px;color:var(--faint);background:var(--sunk);border:1px solid var(--line);border-radius:5px;padding:5px 9px}
.ctawhy{font-size:11.5px;color:var(--faint);flex:1 1 200px}
.inflight{padding:0 16px 13px;font-family:var(--mono);font-size:11.5px;color:var(--pass)}

/* ---------- terminal ---------- */
.term{border-top:1px solid var(--line);background:var(--sunk)}
.termhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid var(--line)}
.termhead .tdot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--go);animation:oppulse 1.6s ease-in-out infinite}
.term.done .termhead .tdot{background:var(--pass);animation:none}
.term.bad .termhead .tdot{background:var(--fail);animation:none}
.term.waiting .termhead .tdot{background:var(--warn)}
.termtitle{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);font-weight:600}
.termhead code{font-family:var(--mono);font-size:11px;color:var(--faint);overflow-x:auto;white-space:nowrap;flex:1 1 200px;scrollbar-width:thin}
.termstatus{font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.term.bad .termstatus{color:var(--fail)}
.term.done .termstatus{color:var(--pass)}
.termout{margin:0;padding:11px 16px;max-height:340px;overflow:auto;font-family:var(--mono);font-size:11.5px;line-height:1.55;color:var(--ink);white-space:pre-wrap;word-break:break-word;scrollbar-width:thin}
.termout:empty{display:none}

/* ---------- prompt dock ---------- */
.prompt{border-top:1px solid var(--line);padding:13px 16px 15px;background:rgba(216,163,90,.06)}
.prompt.secret{background:rgba(126,224,165,.06)}
/* The hazard stripes mark the one hard posture wall, but they must not cost
   the operator any legibility: they sit as a low-alpha layer over the panel. */
.prompt.posture{background-color:var(--sunk);background-image:repeating-linear-gradient(135deg,rgba(216,163,90,.11) 0 8px,rgba(216,163,90,0) 8px 16px)}
.prompt.posture .pdetail{color:var(--ink)}
.prompt.posture .psafe{color:var(--dim)}
.ptitle{font-size:14px;font-weight:600;color:var(--ink);display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.ptitle .bell{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--warn);border:1px solid var(--warn);border-radius:3px;padding:1px 6px;font-weight:600}
.prompt.secret .ptitle .bell{color:var(--go);border-color:var(--go)}
.pdetail{font-size:12px;color:var(--dim);margin-top:5px;max-width:70ch}
.praw{display:block;font-family:var(--mono);font-size:11px;color:var(--faint);background:var(--void);border:1px solid var(--line);border-radius:5px;padding:5px 9px;margin-top:8px;overflow-x:auto;white-space:nowrap}
.pform{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:10px}
.pform input{font-family:var(--mono);font-size:14px;letter-spacing:.18em;background:var(--void);border:1px solid var(--faint);border-radius:6px;color:var(--ink);padding:8px 12px;flex:0 1 260px}
.pform input:focus{outline:2px solid var(--go);outline-offset:1px;border-color:var(--go)}
.psafe{font-size:11px;color:var(--dim);margin-top:8px}
.psafe b{color:var(--ink);font-weight:600}

/* ---------- history ---------- */
details.history{margin-top:30px;border:1px solid var(--line);border-radius:9px;background:rgba(26,34,43,.5)}
details.history>summary{padding:11px 16px;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim);cursor:pointer;font-weight:600;list-style:none}
details.history>summary::before{content:"▸ ";color:var(--faint)}
details.history[open]>summary::before{content:"▾ "}
details.history>summary:focus-visible{outline:2px solid var(--gh);outline-offset:-2px}
.historybody{padding:0 14px 16px}
.filterbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:2px 0 6px}
.flabel{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-right:2px}
.fchip{font-family:var(--mono);font-size:10.5px;border:1px solid var(--line);border-radius:4px;padding:2.5px 9px;color:var(--dim);background:none;cursor:pointer;line-height:1.4}
.fchip:hover{border-color:var(--faint);color:var(--ink)}
.fchip:focus-visible{outline:2px solid var(--gh);outline-offset:1px}
.fchip.off{color:var(--faint);text-decoration:line-through;opacity:.6}
.fchip.tool.on{color:var(--ink);border-color:var(--faint)}
.fchip.status.open.on{color:var(--pass);border-color:var(--pass)}
.fchip.status.complete.on{color:var(--gh);border-color:var(--gh)}
.fchip.status.abandoned.on{color:var(--fail);border-color:var(--fail)}
.fchip.clear{color:var(--faint);border-style:dashed}
.fcount{font-family:var(--mono);font-size:10px;color:var(--faint);margin-left:2px}

details.log{border-top:1px solid var(--line)}
details.log summary{padding:9px 16px;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);cursor:pointer;font-weight:600;list-style:none}
details.log summary::before{content:"▸ ";color:var(--faint)}
details.log[open] summary::before{content:"▾ "}
details.log summary:focus-visible{outline:2px solid var(--gh);outline-offset:-2px}
.events{max-height:220px;overflow-y:auto;padding:0 16px 12px;font-family:var(--mono);font-size:11px}
.events div{padding:1.5px 0;color:var(--dim);white-space:nowrap}
.events .ts{color:var(--faint)}
.events .ev-name{color:var(--ink)}
.events .ev-passed{color:var(--pass)}.events .ev-failed{color:var(--fail)}
.done-line{padding:11px 16px;border-top:1px solid var(--line);font-family:var(--mono);font-size:12px;color:var(--dim)}
.done-line.abandoned{color:var(--fail)}
.nextcmd{display:flex;gap:10px;align-items:center;padding:11px 16px;border-top:1px solid var(--line);flex-wrap:wrap}
.nextcmd .label{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);font-weight:600;flex:none}
.nextcmd code{font-family:var(--mono);font-size:12px;background:var(--sunk);border:1px solid var(--line);border-radius:5px;padding:6px 10px;flex:1 1 320px;overflow-x:auto;white-space:nowrap;scrollbar-width:thin}
.empty{border:1px dashed var(--line);border-radius:8px;margin-top:18px;padding:30px 24px;text-align:center;color:var(--dim)}
.empty code{font-family:var(--mono);color:var(--ink)}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:var(--panel);border:1px solid var(--fail);color:var(--fail);border-radius:7px;padding:10px 16px;font-size:12.5px;z-index:60;max-width:80vw;box-shadow:0 12px 34px -18px #000}
footer{margin-top:34px;font-size:11px;color:var(--faint)}
footer code{font-family:var(--mono)}

@media (max-width:760px){
  .rail{flex-direction:column}
  .gate{flex-basis:26px;border:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .gate span{transform:translate(-50%,-50%)}
  .sep{flex-basis:22px;border-left:0;border-top:2px dashed var(--line)}
  .sep.hot{border-top-color:var(--bb)}
  .sep span{transform:translate(-50%,-50%)}
  .runmeta{margin-left:0;width:100%}
  .act{flex-wrap:wrap}
  .actside{align-items:flex-start}
}
</style>
</head>
<body>
<header>
  <div class="masthead">
    <div class="wordmark">EDGE&nbsp;DEPLOY<small>release console</small></div>
    <div class="posture" id="posture" aria-live="polite"></div>
  </div>
  <div class="pstrip" id="pstrip" aria-label="inferred workstation posture"></div>
  <div class="pnote" id="pnote"></div>
</header>

<main class="wrap">
  <div class="banner offline" id="health" role="status" hidden></div>
  <div id="banners"></div>
  <div class="rootline" id="rootline"></div>

  <div class="sechead"><h2>now</h2><span class="sub" id="nowsub"></span></div>
  <div id="stage"></div>

  <details class="history" id="history">
    <summary id="historysummary">run history</summary>
    <div class="historybody">
      <div class="filterbar" id="filterbar" aria-label="filter runs by tool and status"></div>
      <div id="runs"></div>
    </div>
  </details>

  <footer>Every button runs one allowlisted <code>edge_deploy</code> command in the
  watched checkout and streams its output above — the console holds no release
  logic and never writes to a ledger. Secrets are relayed straight to the engine
  process and never stored. Firewall posture stays manual (ADR-0013): the console
  can only tell you which posture a phase needs and wait for you to confirm the
  switch. Bitbucket/Edge lights are TCP-only; the GitHub light is a per-tool
  authenticated, empty <code>git-receive-pack</code> POST. Divergence uses read-only
  <code>ls-remote</code>. Phase git-protocol probes remain authoritative (ADR-0012/0013).</footer>
</main>

<script>
"use strict";

const TOKEN = "__CONSOLE_TOKEN__";

const PHASE_ORDER = ["verify","publish","deploy","tag_bitbucket","tag_github"];
// Capability requirement per phase (ADR-0013): "any" = github read, available
// in every posture; "bb" = bitbucket vpn; "both" = bitbucket + edge vpns;
// "gh" = github write (firewall off).
const PHASE_REQ = {verify:"any", publish:"bb", deploy:"both", tag_bitbucket:"bb", tag_github:"gh"};
const REQ_TAG = {
  any:  `github read`,
  bb:   `bitbucket`,
  both: `bitbucket <span class="cap-edge">+ edge</span>`,
  gh:   `github write`,
};
const REQ_POSTURE = {
  any:  "any posture",
  bb:   "bitbucket-vpn or both-vpns",
  edge: "edge-vpn or both-vpns",
  both: "both-vpns",
  gh:   "firewall-off",
};
const PHASE_LABEL = {verify:"verify", publish:"publish", deploy:"deploy",
                     tag_bitbucket:"tag bitbucket", tag_github:"tag github"};
const PHASE_ACTION = {verify:"verify", publish:"publish", deploy:"deploy",
                      tag_bitbucket:"tag_bitbucket", tag_github:"tag_github"};
const PHASE_BLURB = {
  verify: "Inspect the checkout, confirm GitHub CI, and run the committed tool gate.",
  publish: "Fast-forward Bitbucket main to the reviewed commit.",
  deploy: "Roll the pending Edge Nodes to the published snapshot — this is where the RSA prompt lands.",
  tag_bitbucket: "Push the immutable release tag to Bitbucket and append the audit record.",
  tag_github: "Push the same tag to GitHub and close the run.",
};
// The rail: five stations, two soft VPN joins, and the one hard wall —
// dropping both VPNs for firewall-off before tag_github.
const RAIL = [
  {phase:"verify"},
  {sep:"+ bitbucket vpn", before:"publish", cap:"bb"},
  {phase:"publish"},
  {sep:"+ edge vpn", before:"deploy", cap:"edge"},
  {phase:"deploy"},
  {phase:"tag_bitbucket"},
  {gate:"firewall off", before:"tag_github"},
  {phase:"tag_github"},
];

let tcpCaps = null;        // latest bitbucket/edge TCP inference
let githubWriteAgg = null; // github write aggregate from /api/posture
let readOnly = false;      // --read-only, from /api/runs
let runsData = null;       // /api/runs
let toolsData = null;      // /api/tools, including the environment block
let actionsById = new Map(); // action id -> latest snapshot from /api/actions

function esc(s){
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function shortTs(iso){
  if(!iso) return "";
  const m = String(iso).match(/T(\d\d:\d\d:\d\d)/);
  return m ? m[1] + "Z" : iso;
}
function shortDate(iso){
  if(!iso) return "";
  return String(iso).replace(/T(\d\d:\d\d).*$/, " $1Z");
}
function plural(n, word){ return `${n} ${word}${n === 1 ? "" : "s"}`; }

// Matches edge_deploy.ledger.is_training_ledger: either marker, strict true.
function isTrainingRun(st){
  return st.kind === "training" || st.training === true;
}

function phasePassed(run, phase){
  if(phase === "deploy"){
    const nodes = run.state.phases.deploy;
    return Object.values(nodes).every(n => n.state === "passed");
  }
  const s = run.state.phases[phase].state;
  return s === "passed" || s === "skipped";
}

function nextPhase(run){
  if(run.state.status !== "open") return null;
  for(const p of PHASE_ORDER) if(!phasePassed(run, p)) return p;
  return null;
}

function pendingNodes(run){
  return Object.entries(run.state.phases.deploy)
    .filter(([,n]) => n.state !== "passed").map(([name]) => name).sort();
}

// The command a button will run, exactly as the engine will see it. The
// console sets the working directory to the run's checkout itself, so no
// "cd" is prepended — the checkout is shown separately on the card.
function nextCommand(run, phase){
  // Training cards never emit a production CLI (status.py parity).
  if(isTrainingRun(run.state))
    return "TRAINING ONLY (not a production command)";
  const id = run.state.run_id;
  if(phase === "verify")  return `py -m edge_deploy verify --run ${id}`;
  if(phase === "publish") return `py -m edge_deploy publish-phase --run ${id}`;
  if(phase === "deploy")  return `py -m edge_deploy deploy --run ${id} --nodes ${pendingNodes(run).join(",")}`;
  if(phase === "tag_bitbucket") return `py -m edge_deploy tag-bitbucket --run ${id}`;
  if(phase === "tag_github")    return `py -m edge_deploy tag-github --run ${id}`;
  return "";
}

/* ---------- posture readiness ---------- */
// Honesty marker next to a command: TCP for VPN phases, the write aggregate
// for github-write. TCP cannot see github write, and neither can tell
// baseline from firewall-off.
function tcpReady(req){
  if(req === "bb")   return tcpCaps.bb;
  if(req === "edge") return tcpCaps.edge;
  return tcpCaps.bb && tcpCaps.edge;
}
function readinessHtml(req){
  if(!tcpCaps) return "";
  if(req === "any") return `<span class="readiness ok">runs in any posture</span>`;
  if(req === "gh"){
    if(githubWriteAgg === "ok")   return `<span class="readiness ok">github write ok</span>`;
    if(githubWriteAgg === "fail") return `<span class="readiness blocked">github write unavailable</span>`;
    return `<span class="readiness">github write unknown</span>`;
  }
  return tcpReady(req) ? `<span class="readiness ok">posture ok (tcp)</span>`
                       : `<span class="readiness blocked">switch needed</span>`;
}
function postureReady(req){
  if(req === "any") return true;
  if(!tcpCaps) return null;
  if(req === "gh") return githubWriteAgg === "ok" ? true : (githubWriteAgg === "fail" ? false : null);
  return tcpReady(req);
}

/* ---------- action rows ---------- */
// One row per runnable command: a button, the exact command it will run, the
// posture it needs, and whether that posture looks held right now. Buttons are
// never hard-disabled on posture alone — the engine's own git-protocol probe
// is authoritative, and a red marker that blocks the operator would be worse
// than one that warns.
function actionRow(a){
  const cap = a.cap || "any";
  const needText = cap === "any" ? "any posture" : `needs ${REQ_POSTURE[cap]}`;
  const cls = ["act", a.primary ? "primary" : "", a.danger ? "danger" : ""].filter(Boolean).join(" ");
  const disabled = readOnly || a.disabled || !!a.blockedBy;
  const btnCls = ["run", a.primary ? "primary" : "", a.primary ? "big" : "", a.danger ? "danger" : ""].filter(Boolean).join(" ");
  const why = a.why ? `<div class="ctawhy">${a.why}</div>` : "";
  return `<div class="${cls}">
    <button class="${btnCls}" data-payload="${esc(JSON.stringify(a.payload))}"
      ${disabled ? "disabled" : ""} ${a.confirm ? `data-confirm="${esc(a.confirm)}"` : ""}
      ${a.confirmText ? `data-confirm-text="${esc(a.confirmText)}"` : ""}
      title="${esc(a.cmd)}">${esc(a.label)}</button>
    <div class="actmain">
      <div class="acttext">${a.text}</div>
      ${why}
      <div class="actcmd"><code>${esc(a.cmd)}</code>
        <button class="copy" data-cmd="${esc(a.cmd)}">copy</button></div>
    </div>
    <div class="actside">
      <span class="need ${cap}">${esc(needText)}</span>
      ${a.blockedBy ? `<span class="blocked-why">${esc(a.blockedBy)}</span>` : readinessHtml(cap)}
    </div>
  </div>`;
}

/* ---------- refusals the console can see coming ---------- */
// The engine turns a command away for several reasons that are already on
// disk or in this process's environment. Showing them here costs nothing and
// saves the operator a refusal that, in a guided release, can arrive several
// phases and a posture switch later.
const ALL_RUN_ACTIONS = ["release","verify","publish","deploy","tag_bitbucket","tag_github","abandon"];
const PHASE_ACTION_IDS = ["release","verify","publish","deploy","tag_bitbucket","tag_github"];

function environment(){
  return (toolsData && toolsData.environment) || null;
}
function toolFor(root){
  return ((toolsData && toolsData.tools) || []).find(t => t.root === root) || null;
}
// A console-driven release holds the run lock for its whole life, so the lock
// the card is looking at is often our own child rather than a foreign process.
function consoleIsBusyIn(root){
  for(const a of actionsById.values())
    if(a.root === root && (a.status === "running" || a.status === "starting")) return true;
  return false;
}

// Conditions that come from the console's own environment rather than from any
// one run. Declared once, so the banner, the run cards and the decision card
// cannot drift apart. `phases` is which phases the condition actually stops —
// null when it stops everything.
function environmentProblems(env){
  if(!env) return [];
  const out = [];
  const config = env.operator_config;
  if(config && config.status === "missing")
    out.push({phases: null, short: "no operator config",
      cta: "There is no operator config, so no engine command can run.",
      text: `<b>No operator config at <code>${esc(config.path)}</code>.</b> Every engine command exits
        immediately without it. Only <code>status</code> and the git buttons work.`});
  else if(config && config.status === "invalid")
    out.push({phases: null, short: "operator config invalid",
      cta: "The operator config cannot be read, so no engine command can run.",
      text: `<b>The operator config could not be read</b> (<code>${esc(config.path)}</code>):
        ${esc(config.detail || "")}`});

  if(env.bb_token && env.bb_token.present === false)
    out.push({phases: ["publish", "tag_bitbucket"], short: "no BB_TOKEN",
      cta: "BB_TOKEN is not in this console's environment, so the release would refuse at publish.",
      text: `<b><code>BB_TOKEN</code> is not set in this console's environment</b>, which is the environment
        every command inherits. Publish and tag-bitbucket refuse. Setting it in another shell will not help —
        restart the console from a shell that has it.`});

  const audit = env.audit;
  if(config && config.status === "ok" && audit && !audit.repo)
    out.push({phases: ["publish"], short: "no audit_repo",
      cta: "The operator config does not define audit_repo, so the release would refuse at publish.",
      text: `<b>The operator config does not define <code>audit_repo</code>.</b> Publish appends a redacted
        record to the audit branch and refuses without it.`});
  else if(audit && audit.queued)
    out.push({phases: ["publish"], short: "audit records queued",
      cta: `Unsynchronized audit records are waiting in ${audit.outbox}; publish refuses until they are sent.`,
      text: `<b>Unsynchronized audit records are waiting in <code>${esc(audit.outbox)}</code>.</b>
        Publish refuses until they reach the audit branch.`});

  if(env.powershell && env.powershell.present === false)
    out.push({phases: ["verify"], short: "no powershell",
      cta: "Neither pwsh nor powershell is on PATH, so verify cannot run this tool's committed gate.",
      text: `<b>Neither <code>pwsh</code> nor <code>powershell</code> is on this machine's PATH.</b>
        Verify runs the tool's committed <code>local_check.ps1</code> through it and blocks the release
        without one.`});
  return out;
}

// The parts of the release gate that come from the checkout itself rather than
// from its commits. All of them are read by verify (ADR-0016, inspect_repository).
function sourceProblems(t){
  if(!t) return [];
  const out = [];
  if(t.profile && t.profile.ok === false)
    out.push({phases: ["verify"], short: "profile unusable",
      cta: `This checkout's edge_deploy.yaml cannot be used: ${t.profile.detail}.`,
      text: `<b>This checkout's <code>edge_deploy.yaml</code> cannot be used:</b> ${esc(t.profile.detail)}.
        Verify reads the same file and stops on it.`});
  if(t.remotes && t.remotes.ok === false)
    out.push({phases: ["verify"], short: "wrong remote",
      cta: `A git remote does not match this tool's edge_deploy.yaml: ${t.remotes.detail}.`,
      text: `<b>A git remote does not match this tool's <code>edge_deploy.yaml</code>:</b>
        ${esc(t.remotes.detail)}. Verify refuses rather than publish somewhere unexpected.`});
  if(t.local_check === false)
    out.push({phases: ["verify"], short: "no local_check.ps1",
      cta: "This checkout has no tools/dev/local_check.ps1, which verify runs as the tool's own gate.",
      text: `<b>This checkout has no <code>tools/dev/local_check.ps1</code>.</b> Verify runs the tool's own
        committed gate and blocks the release when it is missing.`});
  // CI is reported, not predicted: an unknown never blocks, because it needs
  // the network and can change between this answer and the click.
  const ci = t.ci;
  if(ci && (ci.status === "failed" || ci.status === "pending" || ci.status === "missing"))
    out.push({phases: ["verify"], short: `ci ${ci.status}`,
      cta: `GitHub CI is not green for this commit: ${ci.detail}.`,
      text: `<b>GitHub CI is not green for this commit:</b> ${esc(ci.detail)}. Verify requires a successful
        post-merge run for the exact SHA${ci.status === "pending" ? " — waiting may be all this needs" : ""}.`});
  return out;
}

// The console drives Paramiko nodes only (ADR-0018): a pane node's passcode is
// typed in the tmux pane, which the console can neither see nor answer.
function paneNodes(nodes, env){
  const transports = (env && env.operator_config && env.operator_config.nodes) || {};
  return (nodes || []).filter(node => transports[node] === "pane").sort();
}

// Turn a condition into the buttons it stops for THIS run. A condition that
// only affects phases already behind us stops nothing: "Resume guided release"
// on a run waiting at tag_github is not blocked by a missing BB_TOKEN, because
// tag-github pushes to GitHub with the git credential helper.
function scopeToRun(run, problem){
  if(problem.phases === null)
    return {blocks: ALL_RUN_ACTIONS, short: problem.short, text: problem.text};
  const ahead = problem.phases.filter(phase => !phasePassed(run, phase));
  if(!ahead.length) return null;
  return {
    blocks: [...ahead.map(phase => PHASE_ACTION[phase]), "release"],
    short: problem.short,
    text: problem.text,
  };
}

function runBlockers(run, env, tool, consoleHoldsLock){
  const st = run.state, out = [];
  if(run.lock)
    out.push({blocks: ALL_RUN_ACTIONS, short: "run is locked",
      text: consoleHoldsLock
        ? `<b>The command running above holds this run's lock.</b> Everything else on this run waits for it;
           stopping it releases the lock.`
        : `<b>Another process holds this run's lock</b> (pid ${esc(run.lock.pid)} on ${esc(run.lock.hostname)}).
           Every phase refuses, and so does abandon. If that process is gone, release it from a terminal with
           <code>--force-lock</code> — the console deliberately cannot steal a lock.`});

  const runSha = st.engine && st.engine.content_sha256;
  const live = env && env.engine;
  if(!runSha)
    // enter_phase indexes engine.content_sha256 directly and dies on a KeyError.
    out.push({blocks: PHASE_ACTION_IDS, short: "no engine identity",
      text: `<b>This run's ledger records no engine identity.</b> Every phase reads it on entry and stops
        without it; the run has to be abandoned and recreated.`});
  else if(live && live.status === "ok" && live.content_sha256 && runSha !== live.content_sha256)
    out.push({blocks: PHASE_ACTION_IDS, short: "engine mismatch",
      text: `<b>This run belongs to a different engine build.</b> It was created by
        <code>${esc(runSha.slice(0,8))}</code> and the console would run <code>${esc(live.content_sha256.slice(0,8))}</code>.
        Every phase refuses on Engine Identity: finish it with the engine that created it, or abandon it.`});

  for(const problem of environmentProblems(env).concat(sourceProblems(tool))){
    const scoped = scopeToRun(run, problem);
    if(scoped) out.push(scoped);
  }

  // Only verify re-inspects the checkout; once it has passed the engine reuses
  // its evidence, so these stop applying to the rest of the run.
  if(tool && !phasePassed(run, "verify")){
    if(tool.on_main === false)
      out.push({blocks: ["verify","release"], short: "not on main",
        text: `<b>The checkout is on ${esc(tool.branch)}, not main.</b> Verify re-inspects the repository and refuses.`});
    else if(tool.dirty === true)
      out.push({blocks: ["verify","release"], short: "tree not clean",
        text: `<b>The checkout has uncommitted changes.</b> Verify requires a clean working tree.`});
    else if(tool.head && st.kind === "release" && tool.head !== st.source_sha)
      out.push({blocks: ["verify","release"], short: "checkout drift",
        text: `<b>The checkout has moved off this run's source.</b> The run expects
          <code>${esc(st.source_sha.slice(0,7))}</code> and the checkout is at <code>${esc(tool.head.slice(0,7))}</code>.
          Switch the checkout back to the reviewed commit, or abandon the run.`});
  }

  const config = env && env.operator_config;
  const configured = config && config.nodes;
  const runNodes = Object.keys(st.phases.deploy || {});
  if(configured && Object.keys(configured).length && !phasePassed(run, "deploy")){
    const missing = runNodes.filter(n => !(n in configured));
    if(missing.length)
      out.push({blocks: ["deploy","release"], short: "unknown node",
        text: `<b>${esc(missing.join(", "))} ${missing.length === 1 ? "is" : "are"} no longer in the operator config.</b>
          Deploy resolves node names against it and stops on the first one it does not know.`});
    const pane = paneNodes(runNodes, env);
    if(pane.length)
      out.push({blocks: ["deploy","release"], short: "pane transport",
        text: `<b>${esc(pane.join(", "))} ${pane.length === 1 ? "uses" : "use"} the tmux pane transport.</b>
          Its RSA passcode is typed in the pane, which the console cannot see or answer — run this deploy
          from a terminal.`});
  }
  return out;
}

function blockersHtml(blockers){
  if(!blockers.length) return "";
  return `<ul class="blockers" role="status">` +
    blockers.map(b => `<li><span>${b.text}</span></li>`).join("") + `</ul>`;
}

function runActions(run, blockers, tool, consoleHoldsLock){
  const st = run.state;
  if(isTrainingRun(st) || st.status !== "open") return [];
  const id = st.run_id, root = run.root, next = nextPhase(run);
  const reasons = (action, ignoreLock) => {
    const hit = (blockers || []).filter(b =>
      b.blocks.includes(action) && !(ignoreLock && b.short === "run is locked"));
    return hit.length ? hit.map(b => b.short).join(" · ") : null;
  };
  const blocked = action => reasons(action, false);
  const rows = [];
  rows.push({
    label: "▶ Resume guided release",
    primary: true,
    cap: "any",
    text: "Run every remaining phase in one go. It stops here for each RSA passcode and each posture boundary, and you answer without leaving this page.",
    cmd: `py -m edge_deploy release --guided --run ${id}`,
    payload: {action:"release", root, run_id:id},
    blockedBy: blocked("release"),
  });
  if(next){
    const cap = PHASE_REQ[next];
    rows.push({
      label: `Run ${PHASE_LABEL[next]} only`,
      cap,
      text: PHASE_BLURB[next],
      cmd: nextCommand(run, next),
      payload: next === "deploy"
        ? {action:"deploy", root, run_id:id, nodes:pendingNodes(run)}
        : {action:PHASE_ACTION[next], root, run_id:id},
      blockedBy: blocked(PHASE_ACTION[next]),
    });
  }
  // Deep smoke is the only thing that asks for a Kerberos password, so it is
  // offered only where the tool profile declares one.
  if(next === "deploy" && tool && tool.deep_smoke)
    rows.push({
      label: "Run deploy with deep smoke",
      cap: PHASE_REQ.deploy,
      text: "Adds this tool's deep smoke checks, which need a Kerberos ticket — the console relays that prompt too.",
      cmd: `${nextCommand(run, "deploy")} --smoke deep`,
      payload: {action:"deploy", root, run_id:id, nodes:pendingNodes(run), smoke:"deep"},
      blockedBy: blocked("deploy"),
    });
  // The one way past a lock the console otherwise refuses to touch.
  if(run.lock && !consoleHoldsLock)
    rows.push({
      label: "Take the lock and resume",
      danger: true,
      cap: "any",
      text: `Steals the run lock held by pid ${run.lock.pid} on ${run.lock.hostname} and resumes the guided
             release. Only do this once you know that process is gone — two engines in one run corrupt it.`,
      cmd: `py -m edge_deploy release --guided --run ${id} --force-lock`,
      payload: {action:"release", root, run_id:id, force_lock:true},
      confirm: "yes-no",
      confirmText: `The lock on ${id} is held by pid ${run.lock.pid} on ${run.lock.hostname}. `
                 + `Take it anyway? Two engines in one run corrupt it.`,
      // Every other reason still applies — this row only ignores the lock.
      blockedBy: reasons("release", true),
    });
  // status reads local ledgers only: no config, no lock, no engine identity.
  // It is the one command that still works when everything else refuses.
  rows.push({
    label: "Engine status",
    cap: "any",
    text: "Ask the engine for its own view of this checkout — useful when you want to confirm what the console is showing.",
    cmd: "py -m edge_deploy status",
    payload: {action:"status", root},
  });
  rows.push({
    label: "Abandon run",
    danger: true,
    cap: "any",
    text: "Close this run with a recorded reason. Nothing already deployed is rolled back (ADR-0003).",
    cmd: `py -m edge_deploy abandon --run ${id} --reason "<why>"`,
    payload: {action:"abandon", root, run_id:id},
    confirm: "reason",
    blockedBy: blocked("abandon"),
  });
  return rows;
}

/* ---------- rail ---------- */
function stateChip(s){
  // shortTs returns its input unchanged when the timestamp is not the shape it
  // expects, and everything here comes off disk — so it still needs escaping.
  return `<span class="state ${esc(s.state)}"><span class="dot"></span>${esc(s.state)}</span>` +
         (s.updated_at ? `<span class="when">${esc(shortTs(s.updated_at))}</span>` : "");
}

function stationHtml(run, phase, next, live){
  const req = PHASE_REQ[phase];
  // Training rails stay educational: never apply the live "◀ next" cue.
  const training = isTrainingRun(run.state);
  let cls = "station";
  if(!training && phase === live) cls = "station live";
  else if(!training && phase === next) cls = "station next";
  let body;
  if(phase === "deploy"){
    // Deploy node keys are operator-config names ("node03"), and evidence is
    // the node's compact rollout report (release._compact_rollout):
    // state_left says what a failure left behind; drift/smoke are
    // "passed" | "failed" | "not_run".
    const nodes = run.state.phases.deploy;
    body = `<div class="nodes">` + Object.keys(nodes).sort().map(name => {
      const n = nodes[name];
      const ev = n.evidence || {};
      const hint = [
        ev.state_left,
        ev.drift && ev.drift !== "not_run" ? `drift ${ev.drift}` : "",
        ev.smoke && ev.smoke !== "not_run" ? `smoke ${ev.smoke}` : "",
      ].filter(Boolean).join(" · ");
      const title = hint ? ` title="${esc(hint)}"` : "";
      return `<span class="state ${esc(n.state)}"${title}><span class="dot"></span>${esc(name)} · ${esc(n.state)}</span>`;
    }).join("") + `</div>`;
  } else {
    const p = run.state.phases[phase];
    body = stateChip(p);
    if(phase === "publish" && p.evidence && p.evidence.snapshot_sha){
      body += `<div style="font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:5px">snapshot ${esc(p.evidence.snapshot_sha.slice(0,7))}</div>`;
    }
  }
  return `<div class="${cls}" data-req="${req}">
    <span class="posture-tag">${REQ_TAG[req]}</span>
    <h4>${esc(PHASE_LABEL[phase])}</h4>${body}</div>`;
}

// Which station the engine is standing on right now, from release-progress.json.
// Only the deploy phase builds a progress tracker, so every value it writes —
// including "verify", which is the per-node drift/smoke gate run after each
// rollout — belongs to the deploy station.
function livePhase(run){
  const a = run.progress && run.progress.active;
  if(!a || run.state.status !== "open") return null;
  const p = String(a.phase || "");
  return ["rollout", "auth", "deploy", "verify", "publish"].includes(p) ? "deploy" : null;
}

function railHtml(run){
  const next = nextPhase(run);
  const live = livePhase(run);
  const training = isTrainingRun(run.state);
  const parts = RAIL.map(item => {
    if(item.phase) return stationHtml(run, item.phase, next, live);
    if(item.gate){
      // The one hard wall: firewall-off drops both VPNs (ADR-0013). TCP
      // cannot see github write, so this stays hot as a position marker —
      // except on training rails, which must never cue a live switch.
      const hot = !training && next === item.before;
      const aria = training
        ? "simulated firewall-off boundary (do not switch posture)"
        : "drop VPNs, firewall off";
      return `<div class="gate${hot ? " hot" : ""}" role="separator" aria-label="${esc(aria)}"><span>${esc(item.gate)}</span></div>`;
    }
    // A VPN join only glows when the run is waiting here AND that VPN is
    // actually down as far as TCP can see. Training never lights this.
    const missing = !tcpCaps || !tcpCaps[item.cap];
    const hot = !training && next === item.before && missing;
    const aria = training
      ? `simulated ${item.sep} boundary (do not switch posture)`
      : `join ${item.sep}`;
    return `<div class="sep${hot ? " hot" : ""}" role="separator" aria-label="${esc(aria)}"><span>${esc(item.sep)}</span></div>`;
  });
  if(training){
    return `<div class="rail-wrap simulated">
      <div class="rail-sim-tag" role="status">TRAINING ONLY · simulated posture rail — do not switch workstation posture</div>
      <div class="rail simulated" aria-label="TRAINING ONLY simulated posture rail">${parts.join("")}</div>
    </div>`;
  }
  return `<div class="rail">${parts.join("")}</div>`;
}

function eventsHtml(run){
  if(!run.events.length) return "";
  const rows = run.events.slice().reverse().map(e => {
    const cls = /failed|refused|blocked|abandoned|stolen/.test(e.event) ? "ev-failed"
              : /passed|completed|ok/.test(e.event) ? "ev-passed" : "";
    const where = [e.phase, e.node && `node ${e.node}`].filter(Boolean).join(" · ");
    const extra = Object.entries(e)
      .filter(([k]) => !["ts","event","phase","node"].includes(k) && e[k] != null)
      .map(([k,v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
    return `<div><span class="ts">${esc(shortTs(e.ts))}</span>  ` +
           `<span class="ev-name ${cls}">${esc(e.event)}</span>` +
           (where ? `  <span>${esc(where)}</span>` : "") +
           (extra ? `  <span class="ts">${esc(extra)}</span>` : "") + `</div>`;
  }).join("");
  return `<details class="log" data-key="${esc(run.state.run_id)}">
    <summary>event log · last ${run.events.length}</summary>
    <div class="events">${rows}</div></details>`;
}

// The live operation from release-progress.json (ADR-0014): what the engine is
// doing right now (auth <node>, publish <tool>, rollout <tool>/<node>),
// whether it is waiting on the operator (RSA prompt), a stall warning, and any
// in-flight verified binary transfer. A crashed process can leave this stale,
// so the last-update time is always shown.
function progressHtml(run){
  const progress = run.progress;
  if(!progress || !progress.active || run.state.status !== "open") return "";
  const a = progress.active;
  let cls = "activeop", note = "";
  if(a.waiting_on === "operator"){
    cls += " waiting";
    note = "waiting for operator";
  } else if(progress.stall_warning){
    cls += " stalled";
    note = `stalled · no activity for ${Math.round(progress.inactive_s || 0)}s`;
  }
  let html = `<div class="${cls}">
    <span class="op-dot"></span>
    <span class="op-label">${esc(a.label || a.phase || "working")}</span>` +
    (note ? `<span class="op-note">${esc(note)}</span>` : "") +
    `<span class="op-when">updated ${esc(shortTs(progress.updated_at))}</span>
  </div>`;
  const transfer = a.transfer;
  if(transfer){
    const percent = Math.max(0, Math.min(100, transfer.percent));
    const mibSent = (transfer.bytes_sent / (1024*1024)).toFixed(1);
    const mibTotal = (transfer.total_bytes / (1024*1024)).toFixed(1);
    const rate = (transfer.bytes_per_second / (1024*1024)).toFixed(2);
    html += `<div class="transfer">
      <span class="transfer-artifact">${esc(transfer.artifact)}</span>
      <div class="transfer-bar"><div class="transfer-fill" style="width:${percent}%"></div></div>
      <span class="transfer-stats">${esc(mibSent)}/${esc(mibTotal)} MiB · ${esc(percent.toFixed(1))}% · ${esc(rate)} MiB/s</span>
    </div>`;
  }
  return html;
}

// A completed release is the thing you roll back *to*, so the offer belongs on
// its own card — the history is exactly the list of tags worth restoring.
function releaseTagOf(st){
  for(const phase of ["tag_github", "tag_bitbucket"]){
    const tag = st.phases[phase] && st.phases[phase].evidence && st.phases[phase].evidence.tag;
    if(tag) return tag;
  }
  return null;
}

function rollbackHtml(run, opts){
  const st = run.state;
  const tag = releaseTagOf(st);
  if(!tag || opts.rootHasOpenRun) return "";
  return `<div class="actions">${actionRow({
    label: "Roll back to this release",
    danger: true,
    cap: "any",
    text: `Creates a rollback run that restores <code>${esc(tag)}</code> across the Edge Nodes. Nothing is
           rewound on Bitbucket; a rollback is a forward deployment of an older reviewed commit (ADR-0003).`,
    cmd: `py -m edge_deploy rollback --tag ${tag}`,
    payload: {action: "rollback", root: run.root, tag},
    confirm: "yes-no",
    confirmText: `Roll the Edge Nodes back to ${tag}? This starts a new run.`,
  })}</div>`;
}

function runHtml(run, opts){
  opts = opts || {};
  const st = run.state;
  const next = nextPhase(run);
  const training = isTrainingRun(st);
  const statusChip = `<span class="chip ${esc(st.status)}">${esc(st.status)}</span>`;
  const lockChip = run.lock
    ? `<span class="chip lock" title="acquired ${esc(run.lock.acquired_at || "")}">locked · pid ${esc(run.lock.pid)} @ ${esc(run.lock.hostname)}</span>`
    : "";
  // Either marker gets an explicit accessible TRAINING chip; other non-release
  // kinds (rollback, …) keep the generic kind chip.
  const trainingChip = training
    ? `<span class="chip training" role="status" aria-label="TRAINING">training</span>`
    : "";
  const kindChip = (!training && st.kind !== "release")
    ? `<span class="chip">${esc(st.kind)}</span>` : "";
  const rollback = st.rollback_tag
    ? `<span class="chip" title="restores this recorded release tag">→ ${esc(st.rollback_tag)}</span>` : "";

  let tail;
  if(training && st.status === "open"){
    // Label + guidance are TRAINING ONLY — never a production verify/publish/
    // deploy/tag/abandon/release command, and never a runnable button.
    const guidance = next
      ? nextCommand(run, next)
      : "TRAINING ONLY — training ledger awaits completion (not a production command)";
    tail = `<div class="nextcmd">
      <span class="label">TRAINING ONLY</span>
      <span class="need any">${next ? "simulated practice" : "ledger open"}</span>
      <code>${esc(guidance)}</code>
    </div>`;
  } else if(st.status === "open"){
    const tool = toolFor(run.root), busy = consoleIsBusyIn(run.root);
    const blockers = runBlockers(run, environment(), tool, busy);
    const rows = runActions(run, blockers, tool, busy);
    tail = blockersHtml(blockers) +
      (rows.length ? `<div class="actions">${rows.map(actionRow).join("")}</div>` : "");
  } else if(st.status === "abandoned"){
    tail = `<div class="done-line abandoned">abandoned — ${esc(st.abandon_reason || "no reason recorded")}</div>`;
  } else if(training){
    tail = `<div class="done-line">complete — TRAINING ONLY practice finished (not a production release)</div>`;
  } else {
    tail = `<div class="done-line">complete — release-tagged on GitHub and Bitbucket</div>`
         + rollbackHtml(run, opts);
  }

  const slot = opts.spotlight ? `<div class="term-slot" data-root="${esc(run.root || "")}"></div>` : "";
  const cls = ["run", opts.spotlight ? "spot" : "", st.status === "open" ? "" : "closed"].filter(Boolean).join(" ");
  return `<article class="${cls}">
    <div class="runhead">
      <span class="runid">${esc(st.run_id)}</span>
      <span class="chip tool">${esc(st.tool)}</span>${trainingChip}${kindChip}${statusChip}${lockChip}${rollback}
      <span class="runmeta">source ${esc(st.source_sha.slice(0,7))} · ${esc(st.operator)} · engine ${esc(st.engine && st.engine.version || "?")} · ${esc(shortDate(st.created_at))}${run.root ? ` · ${esc(run.root)}` : ""}</span>
    </div>
    ${railHtml(run)}
    ${progressHtml(run)}
    ${tail}
    ${slot}
    ${eventsHtml(run)}
  </article>`;
}

/* ---------- posture panel ---------- */
const POSTURE_NAMES = ["baseline","edge-vpn","bitbucket-vpn","both-vpns","firewall-off"];

function githubWriteHtml(g){
  const agg = g.aggregate || "unknown";
  const rows = (g.tools || []).map(t => {
    const st = t.status || "unknown";
    return `<div class="endpoint ${esc(st)}"><span class="dot"></span>${esc(t.tool)} · ${esc(st)}${t.detail ? ` · ${esc(t.detail)}` : ""}</div>`;
  }).join("") || `<div class="endpoint unknown"><span class="dot"></span>no watched tools</div>`;
  return `<div class="pgroup gh"><h3>github write · ${esc(agg)}</h3>${rows}</div>`;
}

function postureHtml(p){
  const order = ["github","bitbucket","edge"];
  const cls = {github:"gh", bitbucket:"bb", edge:"edge"};
  return order.filter(g => p.groups[g]).map(g => {
    if(g === "github") return githubWriteHtml(p.groups.github);
    const rows = p.groups[g].map(e =>
      `<div class="endpoint ${e.reachable ? "up" : "down"}"><span class="dot"></span>${esc(e.endpoint)}</div>`
    ).join("");
    return `<div class="pgroup ${cls[g]}"><h3>${esc(g)}</h3>${rows}</div>`;
  }).join("");
}

// VPN chips come from Bitbucket/Edge TCP only. GitHub write is a separate
// aggregate (ADR-0013); baseline and firewall-off stay indistinguishable via TCP.
function inferPostures(p){
  const up = g => (p.groups[g] || []).some(e => e.reachable);
  const bb = up("bitbucket"), edge = up("edge");
  if(bb && edge) return {certain:["both-vpns"], bb, edge};
  if(bb) return {certain:["bitbucket-vpn"], bb, edge};
  if(edge) return {certain:["edge-vpn"], bb, edge};
  return {certain:[], maybe:["baseline","firewall-off"], bb, edge};
}

function pstripHtml(inf){
  const maybe = inf.maybe || [];
  return POSTURE_NAMES.map(name => {
    const cls = inf.certain.includes(name) ? "pchip on" : maybe.includes(name) ? "pchip maybe" : "pchip";
    return `<span class="${cls}">${esc(name)}</span>`;
  }).join("");
}

function postureNote(p, inf){
  let read;
  if(inf.bb && inf.edge)
    read = "<b>Both VPNs up</b> — publish, deploy, and tag-bitbucket can run; tag-github still needs the firewall off. GitHub write aggregate is shown separately.";
  else if(inf.bb)
    read = "<b>Bitbucket VPN up</b> — publish and tag-bitbucket can run; deploy also needs the Edge VPN.";
  else if(inf.edge)
    read = "<b>Edge VPN up</b> — join the Bitbucket VPN before publish or deploy.";
  else
    read = "<b>No VPNs reachable</b> — baseline or firewall-off; GitHub write aggregate is shown separately. GitHub read works in every posture.";
  return `${read} <span style="color:var(--faint)">probed ${esc(p.probed_at)}</span>`;
}

/* ---------- the decision card: should I release this tool? ---------- */
const VERDICT_CHIP = {
  up_to_date:     ["up to date", "ok"],
  diverged:       ["release suggested", "warn"],
  checkout_stale: ["pull needed", "warn"],
  never_released: ["never released", "warn"],
  unknown:        ["no git", "dim"],
};

function sha7(sha){ return sha ? String(sha).slice(0, 7) : "?"; }

// Which way the checkout and GitHub main disagree, when git can tell.
function staleReadHtml(t){
  if(t.stale_direction === "local_ahead")
    return `${plural(t.ahead_of_origin, "commit")} not on github main yet — push/PR first (verify needs green CI on HEAD)`;
  if(t.stale_direction === "local_behind")
    return `github main is ahead by ${plural(t.behind_origin, "commit")} — pull for the full picture`;
  if(t.stale_direction === "forked")
    return `checkout and github main have forked (${plural(t.ahead_of_origin, "commit")} local-only, ` +
           `${plural(t.behind_origin, "commit")} remote-only) — reconcile first`;
  return `github main differs — sync the checkout (git can't tell which side is ahead without a fetch)`;
}

// One sentence: what the operator should do about this tool, and why.
function headlineFor(t){
  if(t.verdict === "unknown")
    return {text:"Git state unavailable — is this directory a tool checkout?", calm:true};
  // Lead with the thing that stops a release outright: comparing commits is
  // beside the point when the checkout is not a releasable one.
  if(t.on_main === false)
    return {text:`This checkout is on ${t.branch}, not main — nothing can be released from it.`, calm:false};
  if(t.dirty === true)
    return {text:"This checkout has uncommitted changes — nothing can be released from it until the tree is clean.", calm:false};
  if(t.verdict === "up_to_date")
    return {text:"Nothing to release. The Edge Nodes, this checkout, and GitHub main are all on the same commit.", calm:true};
  if(t.verdict === "never_released")
    return {text:"No completed release is recorded for this tool. A first release would set the deployed baseline.", calm:false};
  if(t.verdict === "checkout_stale")
    return {text:`The Edge Nodes match this checkout, but GitHub main has moved — ${staleReadHtml(t)}.`, calm:false};
  const n = t.ahead == null ? "Reviewed commits are" : `${plural(t.ahead, "reviewed commit")} ${t.ahead === 1 ? "is" : "are"}`;
  const bound = t.stale && t.stale_direction !== "local_ahead" ? "at least " : "";
  return {text:`${bound}${n} on this checkout but not on the Edge Nodes.`, calm:false};
}

function evidenceHtml(t){
  const deployed = t.deployed
    ? `<b>${esc(sha7(t.deployed.sha))}</b><span>${esc(t.deployed.kind)} · ${esc(shortDate(t.deployed.created_at))}</span>`
    : `<b>none</b><span class="warn">no completed run</span>`;
  let checkoutNote = "";
  if(t.verdict === "diverged")
    checkoutNote = `<span class="warn">${esc((t.ahead == null ? "" : (t.stale && t.stale_direction !== "local_ahead" ? "≥ " : "") + plural(t.ahead, "commit") + " ") + "undeployed")}</span>`;
  else if(t.verdict === "up_to_date")
    checkoutNote = `<span class="ok">matches the nodes</span>`;
  const originNote = !t.origin_main
    ? `<span class="warn">unreachable</span>`
    : (t.stale ? `<span class="warn">differs from checkout</span>` : `<span class="ok">in sync with checkout</span>`);
  return `<div class="evidence">
    <div class="ev"><h5>on the edge nodes</h5>${deployed}</div>
    <div class="ev"><h5>this checkout</h5><b>${esc(sha7(t.head))}</b>${checkoutNote}</div>
    <div class="ev"><h5>github main</h5><b>${esc(sha7(t.origin_main))}</b>${originNote}</div>
  </div>`;
}

// Preconditions, each with the button that fixes it where one exists.
function checklistHtml(t){
  const items = [];
  // Every checklist button carries the posture it needs, for the same reason
  // the action rows do: git push wants firewall-off, the node probes want the
  // Edge VPN, and finding that out from a failure is a bad way to find out.
  const btn = (label, payload, cmd, cap) =>
    `<span class="checkact">` +
    `<button class="run" data-payload="${esc(JSON.stringify(payload))}" ${readOnly ? "disabled" : ""}
      title="${esc(cmd)}">${esc(label)}</button>` +
    `<code style="font-family:var(--mono);font-size:10.5px;color:var(--faint)">${esc(cmd)}</code>` +
    (cap && cap !== "any" ? `<span class="need ${cap}">needs ${esc(REQ_POSTURE[cap])}</span>` : "") +
    // Only the blocked case earns a marker here: a green tick on every row is
    // noise, and the posture line above already reports the good news.
    (cap && cap !== "any" && postureReady(cap) === false ? readinessHtml(cap) : "") +
    `</span>`;
  const item = (cls, text, action) => items.push(
    `<li class="${cls}"><span class="ctext">${text}</span>` +
    (action ? `<span class="grow"></span>${action}` : "") + `</li>`);

  // The engine's first two gates, before it compares anything: on main, clean.
  if(t.on_main === false)
    item("blocked", `<b>This checkout is on ${esc(t.branch)}.</b> The engine releases from
      <code>main</code> only — switch back before starting.`);
  else if(t.dirty === true)
    item("blocked", `<b>The working tree is not clean.</b> Commit or stash the changes;
      generated reports under <code>edge-deploy/reports/</code> are the only thing the engine ignores.`);
  else if(t.on_main === true && t.dirty === false)
    item("ok", `On <code>main</code> with a clean working tree.`);

  if(t.verdict === "unknown"){
    item("blocked", `<b>No git state.</b> Point the console at a tool checkout with <code>--root</code>.`);
  } else if(!t.stale){
    item("ok", `Checkout is in sync with GitHub main${t.ahead_exact ? " — the undeployed count is live" : ""}.`);
  } else if(t.stale_direction === "local_ahead"){
    item("blocked", `<b>${esc(plural(t.ahead_of_origin, "commit"))} are not on GitHub main.</b>
      The engine requires HEAD to equal github main, and verify needs green CI on it.`,
      btn("Push to GitHub", {action:"git_push", root:t.root}, "git push origin main", "gh"));
  } else if(t.stale_direction === "forked"){
    item("blocked", `<b>Checkout and GitHub main have forked.</b> Reconcile before releasing.`,
      btn("Rebase onto main", {action:"git_rebase", root:t.root}, "git pull --rebase origin main", "any"));
  } else {
    item("blocked", `<b>GitHub main has moved.</b> ${esc(staleReadHtml(t))}`,
      btn("Pull from GitHub", {action:"git_pull", root:t.root}, "git pull --ff-only origin main", "any"));
  }

  const ready = postureReady("both");
  item(ready === true ? "ok" : "manual",
    `Start on <b>both-vpns</b> if you can — the guided release then needs exactly one more switch,
     to firewall-off for tag-github. It pauses and asks at every boundary either way, and posture
     changes stay manual. ${readinessHtml("both")}`);

  const node = (t.nodes && t.nodes[0]) || null;
  if(node){
    item("", `Optional: confirm the node answers before trusting it.`,
      btn(`Preflight ${node}`, {action:"preflight", root:t.root, node},
          `py -m edge_deploy preflight --node ${node}`, "edge"));
    item("", `Optional: exercise the Paramiko transport end to end.`,
      btn(`Smoke ${node}`, {action:"transport_smoke", root:t.root, node},
          `py -m edge_deploy transport-smoke --node ${node}`, "edge"));
    if(t.deployed && t.deployed.sha)
      item("", `Optional: check the node still matches what was last deployed.`,
        btn(`Drift ${node}`,
            {action:"drift", root:t.root, tool:t.tool, node, commit:t.deployed.sha},
            `py -m edge_deploy drift --tool ${t.tool} --node ${node} --commit ${sha7(t.deployed.sha)}`,
            "both"));
  }
  return `<ol class="checklist">${items.join("")}</ol>`;
}

// What stops a release outright, as opposed to what merely needs attention.
// The engine's own gate is exact equality: inspect_repository refuses unless
// HEAD == origin/main, before a run is even created. So every kind of stale
// checkout blocks, not just the two that also lack CI.
function releaseBlocker(t, env){
  // A new release runs every phase, so every environment or source problem
  // applies — same list the run cards use, so the two cannot disagree.
  const problem = environmentProblems(env).concat(sourceProblems(t))[0];
  if(problem) return problem.cta;
  if(t.verdict === "unknown") return "This checkout has no readable git state.";
  // inspect_repository checks the branch and the working tree before it looks
  // at any SHA, so a feature branch sitting exactly on origin/main compares as
  // perfectly in sync and is still refused.
  if(t.on_main === false)
    return `The engine releases from branch main only, and this checkout is on ${t.branch}.`;
  if(t.dirty === true)
    return "The engine requires a clean working tree, and this checkout has uncommitted changes.";
  if(t.stale && t.stale_direction === "local_ahead")
    return "The engine requires HEAD to equal github main, and these commits are not pushed yet — verify would also find no CI result for them.";
  if(t.stale && t.stale_direction === "forked")
    return "The engine requires HEAD to equal github main, and the checkout has forked from it.";
  if(t.stale)
    return "The engine requires HEAD to equal github main, and github main has moved ahead — pull first.";
  if(t.verdict === "up_to_date")
    return "Deployed, checkout, and GitHub main are the same commit — there is nothing to ship.";
  return null;
}

function decisionHtml(t){
  let [chipText, chipCls] = VERDICT_CHIP[t.verdict] || ["?", "dim"];
  if(t.verdict === "checkout_stale" && t.stale_direction === "local_ahead")
    [chipText, chipCls] = ["push needed", "warn"];
  const head = `<div class="dechead">
      <span class="toolname">${esc(t.tool)}</span>
      <span class="verdict ${chipCls}">${esc(chipText)}</span>
      <span class="toolroot">${esc(t.root)}</span>
    </div>`;

  if(t.open_run_id){
    return `<article class="decision" data-suggest="0">${head}
      <div class="inflight">release in flight — ${esc(t.open_run_id)} (spotlighted above)</div>
    </article>`;
  }

  const suggest = ["diverged", "checkout_stale", "never_released"].includes(t.verdict);
  const headline = headlineFor(t);
  const blocker = releaseBlocker(t, environment());
  // aria-describedby, not just proximity: a disabled button announces nothing
  // about why it is disabled unless the reason is wired to it. Keyed by root,
  // because two watched checkouts can be the same tool (training and real).
  const whyId = `why-${t.root.replace(/[^a-z0-9_-]/gi, "-")}`;
  const cta = `<div class="cta">
      <button class="run primary big" data-payload="${esc(JSON.stringify({action:"release", root:t.root, tool:t.tool}))}"
        ${readOnly || blocker ? "disabled" : ""} aria-describedby="${esc(whyId)}"
        title="py -m edge_deploy release --guided">▶ Start guided release</button>
      <code>py -m edge_deploy release --guided</code>
      <button class="copy" data-cmd="py -m edge_deploy release --guided">copy</button>
      <div class="ctawhy" id="${esc(whyId)}">${blocker
        ? esc(blocker)
        : "Walks verify → publish → deploy → tag-bitbucket → tag-github, pausing here for every RSA passcode and posture switch."}</div>
    </div>`;
  return `<article class="decision" data-suggest="${suggest ? 1 : 0}">
    ${head}
    <p class="headline${headline.calm ? " calm" : ""}">${esc(headline.text)}</p>
    ${evidenceHtml(t)}
    ${checklistHtml(t)}
    ${cta}
    <div class="term-slot" data-root="${esc(t.root)}"></div>
  </article>`;
}

/* ---------- terminals: one persistent element per action ---------- */
const terms = new Map();     // action id -> {el, out, cursor, pinned, …}

function termEl(id){
  let t = terms.get(id);
  if(t) return t;
  const el = document.createElement("section");
  el.className = "term";
  el.dataset.id = id;
  el.innerHTML = `<div class="termhead">
      <span class="tdot"></span><span class="termtitle"></span>
      <code></code><span class="termstatus"></span>
      <button class="run term-cancel" data-cancel="${id}">stop</button>
    </div>
    <pre class="termout" tabindex="0"></pre>
    <div class="promptdock"></div>`;
  const out = el.querySelector(".termout");
  // seenPrompt tells the server which question we have already drawn, so a
  // pending prompt does not satisfy every long poll. answered remembers what
  // we have replied to, so an in-flight response cannot redraw a dead prompt.
  t = {el, out, cursor: 0, pinned: true, hydrated: false, seenPrompt: null, answered: new Set()};
  out.addEventListener("scroll", () => {
    t.pinned = out.scrollHeight - out.scrollTop - out.clientHeight < 24;
  });
  terms.set(id, t);
  return t;
}

function appendOutput(id, text, reset){
  const t = termEl(id);
  if(reset){ t.out.textContent = ""; }
  if(text){ t.out.textContent += text; }
  if(t.pinned) t.out.scrollTop = t.out.scrollHeight;
}

function elapsed(a){
  const end = a.finished_at || (Date.now() / 1000);
  const secs = Math.max(0, Math.round(end - a.started_at));
  return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}

// The prompt the operator should be looking at: what the server says is
// pending, minus anything this tab has already answered.
function activePrompt(a, t){
  const p = a && a.prompt;
  if(!p || (t && t.answered.has(p.id))) return null;
  return p;
}

function promptHtml(a, t){
  const p = activePrompt(a, t);
  if(!p) return "";
  const raw = p.raw ? `<code class="praw">${esc(p.raw)}</code>` : "";
  const head = `<div class="ptitle"><span class="bell">engine is waiting</span>${esc(p.title)}</div>
    <div class="pdetail">${esc(p.detail)}</div>${raw}`;
  if(p.kind === "secret"){
    return `<div class="prompt secret" role="alert" data-prompt="${esc(p.id)}" data-action="${esc(a.id)}">
      ${head}
      <div class="pform">
        <input type="password" class="secretbox" autocomplete="off" autocapitalize="off"
          spellcheck="false" aria-label="${esc(p.title)}" placeholder="passcode">
        <button class="run primary" data-answer="secret" disabled>Send to the engine</button>
      </div>
      <div class="psafe">Written straight to the running process stdin. <b>Never stored, logged, echoed back, or sent anywhere else</b> — the transcript above shows only asterisks.</div>
    </div>`;
  }
  if(p.kind === "ack"){
    return `<div class="prompt posture" role="alert" data-prompt="${esc(p.id)}" data-action="${esc(a.id)}">
      ${head}
      <div class="pform">
        <button class="run primary" data-answer="ack">I have switched — continue</button>
        <button class="run" data-cancel="${esc(a.id)}">Stop and resume later</button>
      </div>
      <div class="psafe">The console cannot change your firewall posture. After you continue, the engine re-probes for about 90 seconds before giving up.</div>
    </div>`;
  }
  if(p.kind === "choice"){
    return `<div class="prompt" role="alert" data-prompt="${esc(p.id)}" data-action="${esc(a.id)}">
      ${head}
      <div class="pform">
        <button class="run primary" data-answer="value" data-value="y">Yes</button>
        <button class="run" data-answer="value" data-value="n">No</button>
      </div>
    </div>`;
  }
  return `<div class="prompt" role="alert" data-prompt="${esc(p.id)}" data-action="${esc(a.id)}">
    ${head}
    <div class="pform">
      <input type="text" class="textbox" autocomplete="off" aria-label="${esc(p.title)}">
      <button class="run primary" data-answer="text" disabled>Send</button>
    </div>
  </div>`;
}

function renderTerm(id){
  const t = terms.get(id);
  const a = actionsById.get(id);
  if(!t || !a) return;
  const prompt = activePrompt(a, t);
  const running = a.status === "running" || a.status === "starting";
  const bad = !running && (a.exit_code !== 0 || a.status === "failed");
  t.el.className = "term" + (running ? (prompt ? " waiting" : "") : (bad ? " bad" : " done"));
  t.el.querySelector(".termtitle").textContent = a.label + (a.run_id ? ` · ${a.run_id}` : "");
  t.el.querySelector(".termhead code").textContent = a.command;
  t.el.querySelector(".termstatus").textContent = running
    ? (prompt ? `waiting for you · ${elapsed(a)}` : `running · ${elapsed(a)}`)
    : (a.status === "failed" ? "could not start" : `exit ${a.exit_code} · ${elapsed(a)}`);
  const cancel = t.el.querySelector(".term-cancel");
  cancel.style.display = running ? "" : "none";
  cancel.dataset.cancel = id;

  const dock = t.el.querySelector(".promptdock");
  const shownPrompt = dock.firstElementChild && dock.firstElementChild.dataset.prompt;
  const wantPrompt = prompt && prompt.id;
  if(shownPrompt !== wantPrompt){
    dock.innerHTML = promptHtml(a, t);
    // Land the caret (or the acknowledgement button) where the answer goes,
    // so the keyboard is already in the right place.
    const focusTarget = dock.querySelector("input") || dock.querySelector("[data-answer]");
    if(focusTarget) focusTarget.focus();
  }
}

// Terminals live outside the re-rendered HTML so their text survives every
// poll. Re-parenting a live node still drops focus and scroll, though, and the
// stage re-renders whenever release-progress.json moves — which is exactly
// while a passcode is being typed. Both are captured and restored here.
function placeTerminals(){
  const focused = document.activeElement;
  const caret = focused && focused.tagName === "INPUT"
    ? {el: focused, start: focused.selectionStart, end: focused.selectionEnd} : null;
  const byRoot = new Map();
  for(const a of actionsById.values()){
    const current = byRoot.get(a.root);
    if(!current || a.started_at >= current.started_at) byRoot.set(a.root, a);
  }
  for(const slot of document.querySelectorAll(".term-slot")){
    const a = byRoot.get(slot.dataset.root);
    if(!a){ slot.replaceChildren(); continue; }
    const t = termEl(a.id);
    if(t.el.parentElement !== slot){
      const scroll = t.out.scrollTop;
      slot.replaceChildren(t.el);
      t.out.scrollTop = t.pinned ? t.out.scrollHeight : scroll;
    }
    renderTerm(a.id);
  }
  if(caret && caret.el.isConnected && document.activeElement !== caret.el){
    caret.el.focus();
    try{ caret.el.setSelectionRange(caret.start, caret.end); }catch(_e){ /* not selectable */ }
  }
}

/* ---------- talking to the console ---------- */
function toast(message){
  const existing = document.querySelector(".toast");
  if(existing) existing.remove();
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "alert");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

// A stale token or a dead console must not look like a working page: both put
// a sticky banner up, because a toast that has already faded is no help at 2am.
function setHealth(message){
  const el = document.getElementById("health");
  el.hidden = !message;
  el.innerHTML = message || "";
}

const UNREACHABLE = `Console unreachable — nothing on this page is live. Check the terminal
  that started it, then reload.`;

async function post(url, body){
  let res;
  try{
    res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Edge-Console-Token": TOKEN},
      body: JSON.stringify(body || {}),
    });
  }catch(_e){
    // "Failed to fetch" tells an operator nothing they can act on.
    setHealth(UNREACHABLE);
    throw new Error("The console is not answering. Check the terminal that started it, then reload.");
  }
  const data = await res.json().catch(() => ({}));
  if(!res.ok){
    if(res.status === 403 && /token/.test(data.error || ""))
      setHealth(`<b>This page is out of date</b> — the console has been restarted, so its
        commands are refused. Reload the page to reconnect.`);
    throw new Error(data.error || `request failed (${res.status})`);
  }
  return data;
}

async function startAction(payload){
  const started = await post("/api/actions", payload);
  actionsById.set(started.id, started);
  termEl(started.id);
  placeTerminals();
  pump(started.id, true);
  pollRuns();
}

// Follow a running command, or (follow=false) pull a finished one's transcript
// once — that is what makes a reload, or a second tab, show the whole thing.
const pumping = new Set();
async function pump(id, follow){
  if(pumping.has(id)) return;
  pumping.add(id);
  try{
    for(;;){
      const t = termEl(id);
      const seen = t.seenPrompt ? `&seen=${encodeURIComponent(t.seenPrompt)}` : "";
      const res = await fetch(`/api/actions/${id}/output?cursor=${t.cursor}&wait=${follow ? 20 : 0}${seen}`);
      if(!res.ok) break;
      const data = await res.json();
      t.cursor = data.cursor;
      t.hydrated = true;
      t.seenPrompt = data.prompt ? data.prompt.id : null;
      actionsById.set(id, data);
      appendOutput(id, data.text, data.reset);
      renderTerm(id);
      if(!follow || (data.status !== "running" && data.status !== "starting")) break;
    }
  }catch(_e){ /* server briefly gone; the action list will re-attach */ }
  finally{
    pumping.delete(id);
    if(follow){ pollRuns(); pollTools(); }
  }
}

/* ---------- run filter (history only) ---------- */
const STATUS_ORDER = ["open", "complete", "abandoned"];
const FILTER_STORAGE_KEY = "edge-console-filter-v1";

function loadFilterState(){
  try{
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return {
      excludedTools: new Set(Array.isArray(parsed.excludedTools) ? parsed.excludedTools : []),
      excludedStatuses: new Set(Array.isArray(parsed.excludedStatuses) ? parsed.excludedStatuses : []),
    };
  }catch(_e){
    return {excludedTools: new Set(), excludedStatuses: new Set()};
  }
}
function saveFilterState(){
  try{
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({
      excludedTools: [...filterState.excludedTools],
      excludedStatuses: [...filterState.excludedStatuses],
    }));
  }catch(_e){ /* private mode / storage disabled: filter still works this session */ }
}
let filterState = loadFilterState();

function passesFilter(run){
  const st = run.state;
  if(filterState.excludedStatuses.has(st.status)) return false;
  if(filterState.excludedTools.has(st.tool)) return false;
  return true;
}

function filterBarHtml(allRuns){
  if(!allRuns.length) return "";
  const tools = [...new Set(allRuns.map(r => r.state.tool))].sort();
  const countOf = (kind, value) => allRuns.filter(r =>
    kind === "tool" ? r.state.tool === value : r.state.status === value).length;
  const chip = (kind, value, label) => {
    const on = kind === "tool" ? !filterState.excludedTools.has(value) : !filterState.excludedStatuses.has(value);
    const cls = kind === "tool" ? "tool" : `status ${value}`;
    return `<button class="fchip ${cls} ${on ? "on" : "off"}" data-kind="${kind}" data-value="${esc(value)}"
      aria-pressed="${on}">${esc(label)} <span class="fcount">${countOf(kind, value)}</span></button>`;
  };
  const toolChips = tools.map(t => chip("tool", t, t)).join("");
  const statusChips = STATUS_ORDER.map(s => chip("status", s, s)).join("");
  const active = filterState.excludedTools.size || filterState.excludedStatuses.size;
  const shown = allRuns.filter(passesFilter).length;
  const clear = active
    ? `<button class="fchip clear" id="filter-clear">clear</button><span class="fcount">${shown}/${allRuns.length} shown</span>`
    : "";
  return `<span class="flabel">tool</span>${toolChips}
          <span class="flabel">status</span>${statusChips}${clear}`;
}

/* ---------- render ---------- */
function renderBanners(){
  const parts = [];
  if(runsData && runsData.demo)
    parts.push(`<div class="banner demo"><b>DEMO</b> — fabricated checkouts driven by an offline simulator.
      No network, Edge Node, or real ledger is touched, and the posture lights are canned.</div>`);
  if(readOnly)
    parts.push(`<div class="banner readonly"><b>READ-ONLY</b> — this console was started with
      <code>--read-only</code>, so every command button is disabled. Copy the commands and run them in a terminal.</div>`);

  // Conditions that apply to every watched checkout, from the one list the
  // run cards and the decision cards also read.
  const env = environment();
  for(const problem of environmentProblems(env))
    parts.push(`<div class="banner ${problem.phases === null ? "offline" : "demo"}">${problem.text}</div>`);
  if(env && env.engine && env.engine.status === "unknown")
    parts.push(`<div class="banner offline"><b>The release engine could not be identified</b>
      (${esc(env.engine.detail || "")}). Commands may not run at all; check
      <code>--engine-python</code>.</div>`);
  document.getElementById("banners").innerHTML = parts.join("");
}

function renderStage(){
  if(!runsData) return;
  const el = document.getElementById("stage");
  const open = runsData.runs.filter(r => r.state.status === "open");
  const openRoots = new Set(open.map(r => r.root));
  const tools = (toolsData && toolsData.tools) || [];
  const decisions = tools.filter(t => !openRoots.has(t.root));

  const openLogs = new Set([...el.querySelectorAll("details.log[open]")].map(d => d.dataset.key));
  const parts = open.map(r => runHtml(r, {spotlight: true}))
    .concat(decisions.map(decisionHtml));

  if(!parts.length){
    el.innerHTML = `<div class="empty">Nothing open and no checkout to judge.<br><br>
      Point the console at a tool checkout with <code>--root</code>, or start it with <code>--demo</code>.</div>`;
  } else {
    el.innerHTML = parts.join("");
    for(const d of el.querySelectorAll("details.log")) if(openLogs.has(d.dataset.key)) d.open = true;
  }

  const sub = open.length
    ? `${plural(open.length, "open run")} · ${plural(decisions.length, "other checkout")}`
    : (decisions.length ? `no open run · ${plural(decisions.length, "checkout")} checked` : "");
  document.getElementById("nowsub").textContent = sub;
  placeTerminals();
}

function renderHistory(){
  if(!runsData) return;
  const all = runsData.runs;
  const closed = all.filter(r => r.state.status !== "open");
  document.getElementById("historysummary").textContent =
    closed.length ? `run history · ${plural(closed.length, "closed run")}` : "run history · empty";
  document.getElementById("filterbar").innerHTML = filterBarHtml(closed);
  const el = document.getElementById("runs");
  const openLogs = new Set([...el.querySelectorAll("details.log[open]")].map(d => d.dataset.key));
  const filtered = closed.filter(passesFilter);
  if(!closed.length){
    el.innerHTML = `<div class="empty">No closed runs under the watched checkouts yet.</div>`;
    return;
  }
  if(!filtered.length){
    el.innerHTML = `<div class="empty">${plural(closed.length, "run")} hidden by the filter above.<br><br>
      <button class="fchip clear" id="filter-clear-empty">clear filter</button></div>`;
    return;
  }
  // A rollback creates a run, which the engine refuses while one is open.
  const busyRoots = new Set(all.filter(r => r.state.status === "open").map(r => r.root));
  el.innerHTML = filtered
    .map(r => runHtml(r, {rootHasOpenRun: busyRoots.has(r.root)}))
    .join("");
  for(const d of el.querySelectorAll("details.log")) if(openLogs.has(d.dataset.key)) d.open = true;
}

function renderRoots(){
  if(!runsData) return;
  document.getElementById("rootline").innerHTML =
    `watching ${runsData.roots.map(esc).join(`<span style="color:var(--line)"> · </span>`)}`;
}

/* ---------- polling ---------- */
let lastRuns = "", lastTools = "";

let pollFailures = 0;
async function pollRuns(){
  try{
    const res = await fetch("/api/runs");
    const data = await res.json();
    pollFailures = 0;
    if(document.getElementById("health").textContent.startsWith("Console unreachable")) setHealth("");
    const raw = JSON.stringify(data);
    runsData = data;
    readOnly = !!data.read_only;
    if(raw === lastRuns) return;
    lastRuns = raw;
    renderBanners(); renderRoots(); renderStage(); renderHistory();
  }catch(_e){
    // Everything on screen is now a snapshot of an unknown age; say so rather
    // than letting a dead console look like a live one.
    if(++pollFailures >= 3) setHealth(UNREACHABLE);
  }
}

async function pollTools(){
  try{
    const res = await fetch("/api/tools");
    const data = await res.json();
    const raw = JSON.stringify(data);
    toolsData = data;
    // The environment block rides along here, and the banners depend on it.
    if(raw !== lastTools){ lastTools = raw; renderBanners(); renderStage(); renderHistory(); }
  }catch(_e){ /* keep last render */ }
}

async function pollPosture(){
  try{
    const res = await fetch("/api/posture");
    const p = await res.json();
    const inf = inferPostures(p);
    const nextAgg = p.groups.github && p.groups.github.aggregate;
    const capsChanged = !tcpCaps || tcpCaps.bb !== inf.bb || tcpCaps.edge !== inf.edge
      || githubWriteAgg !== nextAgg;
    tcpCaps = {bb: inf.bb, edge: inf.edge};
    githubWriteAgg = nextAgg;
    document.getElementById("posture").innerHTML = postureHtml(p);
    document.getElementById("pstrip").innerHTML = pstripHtml(inf);
    document.getElementById("pnote").innerHTML = postureNote(p, inf);
    if(capsChanged){ renderStage(); }  // refresh readiness markers
  }catch(_e){ /* ignore */ }
}

// The action list is the reconnect path: a reload, or a second tab, re-attaches
// to whatever is still running (including a pending prompt).
async function pollActions(){
  try{
    const res = await fetch("/api/actions");
    const data = await res.json();
    readOnly = !!data.read_only;
    let changed = false;
    const live = new Set();
    for(const a of data.actions){
      live.add(a.id);
      const before = actionsById.get(a.id);
      actionsById.set(a.id, Object.assign({}, before, a));
      if(!before) changed = true;
      const running = a.status === "running" || a.status === "starting";
      if(running) pump(a.id, true);
      else if(!termEl(a.id).hydrated) pump(a.id, false);
      renderTerm(a.id);
    }
    // The server retains a bounded history; anything it has dropped (or that a
    // restart forgot) must not linger here still claiming to be running. The
    // age check keeps a command started between this request and its response.
    const cutoff = Date.now() / 1000 - 10;
    for(const id of [...actionsById.keys()]){
      const a = actionsById.get(id);
      if(live.has(id) || (a.started_at || 0) > cutoff) continue;
      actionsById.delete(id);
      terms.delete(id);
      changed = true;
    }
    if(changed) placeTerminals();
  }catch(_e){ /* ignore */ }
}

/* ---------- events ---------- */
document.addEventListener("click", async ev => {
  const copyBtn = ev.target.closest("button.copy");
  if(copyBtn){
    try{ await navigator.clipboard.writeText(copyBtn.dataset.cmd); }
    catch(_e){
      const t = document.createElement("textarea");
      t.value = copyBtn.dataset.cmd; document.body.appendChild(t); t.select();
      document.execCommand("copy"); t.remove();
    }
    copyBtn.textContent = "copied"; setTimeout(() => { copyBtn.textContent = "copy"; }, 1400);
    return;
  }

  if(ev.target.closest("#filter-clear, #filter-clear-empty")){
    filterState.excludedTools.clear();
    filterState.excludedStatuses.clear();
    saveFilterState();
    renderHistory();
    return;
  }
  const chipBtn = ev.target.closest(".fchip[data-kind]");
  if(chipBtn){
    const set = chipBtn.dataset.kind === "tool" ? filterState.excludedTools : filterState.excludedStatuses;
    if(set.has(chipBtn.dataset.value)) set.delete(chipBtn.dataset.value); else set.add(chipBtn.dataset.value);
    saveFilterState();
    renderHistory();
    return;
  }

  const cancelBtn = ev.target.closest("[data-cancel]");
  if(cancelBtn){
    try{ await post(`/api/actions/${cancelBtn.dataset.cancel}/cancel`); }
    catch(err){ toast(err.message); }
    return;
  }

  const answerBtn = ev.target.closest("[data-answer]");
  if(answerBtn){
    const dock = answerBtn.closest("[data-prompt]");
    const mode = answerBtn.dataset.answer;
    let value = "";
    if(mode === "secret" || mode === "text"){
      const box = dock.querySelector("input");
      value = box.value;
      box.value = "";
    } else if(mode === "value"){
      value = answerBtn.dataset.value;
    }
    const id = dock.dataset.action, promptId = dock.dataset.prompt;
    answerBtn.disabled = true;
    try{
      // Adopt the answered snapshot straight away, and remember the prompt id:
      // a long poll already in flight still carries the old prompt, and must
      // not redraw a question the operator has answered.
      const answered = await post(`/api/actions/${id}/answer`, {prompt_id: promptId, value});
      termEl(id).answered.add(promptId);
      actionsById.set(id, Object.assign({}, actionsById.get(id), answered));
      dock.remove();
      renderTerm(id);
    }catch(err){
      answerBtn.disabled = false;
      toast(err.message);
    }
    return;
  }

  const runBtn = ev.target.closest("button.run[data-payload]");
  if(runBtn){
    const payload = JSON.parse(runBtn.dataset.payload);
    if(runBtn.dataset.confirm === "reason"){
      const reason = window.prompt("Why is this run being abandoned? The reason is recorded in the ledger.");
      if(!reason) return;
      payload.reason = reason;
    } else if(runBtn.dataset.confirm === "yes-no"){
      if(!window.confirm(runBtn.dataset.confirmText || "Are you sure?")) return;
    }
    runBtn.disabled = true;
    try{ await startAction(payload); }
    catch(err){ toast(err.message); }
    finally{ setTimeout(() => { runBtn.disabled = false; }, 800); }
  }
});

// An empty answer to a passcode prompt reads as "the operator gave up" to the
// engine, and it is far too easy to produce by accident: keep Send off until
// something has been typed.
document.addEventListener("input", ev => {
  const box = ev.target.closest(".prompt input");
  if(!box) return;
  box.closest(".prompt").querySelector("[data-answer]").disabled = !box.value;
});

// Enter submits a prompt without reaching for the mouse.
document.addEventListener("keydown", ev => {
  if(ev.key !== "Enter") return;
  const box = ev.target.closest(".prompt input");
  if(!box) return;
  ev.preventDefault();
  const send = box.closest(".prompt").querySelector("[data-answer]");
  if(!send.disabled) send.click();
});

pollRuns(); pollPosture(); pollTools(); pollActions();
setInterval(pollRuns, 2000);
setInterval(pollActions, 2000);
setInterval(pollPosture, 20000);
setInterval(pollTools, 30000);
// Keep the "running 0:42" clocks honest between polls.
setInterval(() => { for(const id of terms.keys()) renderTerm(id); }, 1000);
</script>
</body>
</html>
"""

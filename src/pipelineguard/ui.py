"""Local inspection UI for the PipelineGuard detectors.

Run from the repo root so Streamlit finds .streamlit/config.toml:

    HF_HUB_OFFLINE=1 python -m streamlit run src/pipelineguard/ui.py \
        --server.address localhost

`--server.address localhost` is not decoration: Streamlit otherwise binds every
interface and prints a LAN-reachable URL. It lives in the command rather than in
.streamlit/config.toml because a hosted build reads that file too.

Local only, by design. docs/decisions.md section 1 rules out a hosted service
that accepts uploads: taking third-party documents would make this project a
data controller, which is the opposite of what it exists to demonstrate.

Presentation only. Everything that computes a value lives in `scan` and
`batch_report`, where the test suite can reach it.
"""
from __future__ import annotations

import os
import warnings

# Set before anything imports huggingface_hub or transformers, which read these
# at import time. Progress bars for a warm cache drown the actual output.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")

import threading  # noqa: E402
from datetime import datetime, time, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import streamlit as st  # noqa: E402
from streamlit_option_menu import option_menu  # noqa: E402

from pipelineguard import batch_report, compliance, report, scan  # noqa: E402
from pipelineguard.config import settings  # noqa: E402
from pipelineguard.detectors.tier1_rules import RulesDetector  # noqa: E402
from pipelineguard.models import Tier  # noqa: E402

# Public demo build. Set on the hosted app and nowhere else; every difference
# it makes is listed in decisions.md rather than scattered through this file.
DEMO = os.getenv("PG_DEMO", "").strip().lower() in {"1", "true", "yes"}

CONTACT_EMAIL = "affanbasra12@gmail.com"
REPO_URL = "https://github.com/AffanBasra/PipelineGuard"

# A stored run of the real report, for the demo, which has no database behind
# it. Regenerate with scripts/make_sample_summary.py after a pipeline run.
SAMPLE_SUMMARY = Path(__file__).resolve().parents[2] / "docs" / "sample-summary.md"
SAMPLE_RECORDS = "5,000"

# 16.2 ms/record for the shipped two-group encoder config on the reference GPU
# (docs/tier2-detection-findings.md sections 11 and 18). 500 rows is ~8 seconds;
# 2000 would be ~32, which reads as a hang.
#
# The demo runs bf16 on a shared CPU box at ~225 ms/record (findings §27.5), and
# detection takes a global lock, so one big scan stalls every other visitor.
# Half the fp32 rows for the same ~11 s wait: the memory saving costs latency.
MAX_ROWS = 50 if DEMO else 500

# A dead broker is not refused, it is unanswered, so psycopg waits out its
# default timeout -- 10s of a UI that looks broken. Fail fast and say why.
DB_TIMEOUT_S = 5

PRIVACY_NOTICE = (
    "**Your file never leaves this page.** It is read into memory, scanned, "
    "and dropped when the scan finishes. It is never written to disk, never "
    "logged, and never added to the audit database. Only the redacted output "
    "and the counts stay on screen.\n\n"
    "This is a demonstration on shared free hosting. Please use synthetic "
    "data. Nothing here is a compliance determination.\n\n"
    f"Questions, or want it taken down? [{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL}) "
    f"· [source on GitHub]({REPO_URL})"
)

RULE_TYPES = ["CNIC", "IBAN_PK", "PHONE_PK", "EMAIL"]
ENCODER_TYPES = ["PERSON_NAME", "ADDRESS"]

TIER_LABEL = {Tier.RULES: "Tier 1 (rules)", Tier.ENCODER: "Tier 2 (encoder)"}

# Cases with a documented result, so the app shows behaviour that is written
# down rather than whatever happens to be typed. Every label below was checked
# against this checkpoint at threshold 0.55 -- including the two that are
# failures, which are labelled as failures.
PRESETS = {
    "Full stack — rules and encoder together":
        "Transfer to Ayesha Malik, CNIC 42101-1234567-8, "
        "account PK68MEZN5748718428058488, contact 0300 1234567",
    "Karachi plot — bridged across an interior gap (§23)":
        "Deliver to C-21, Block J, North Nazimabad, Karachi",
    "Roman Urdu — city trails the address (§24)":
        "Statement Plot E-379, Airport Road, Quetta par bhej dein",
    "Islamabad address in a code-switched memo":
        "Ghar 12, Gali 4, F-8/3 Islamabad par bhej dein",
    "False positive — a religious term read as a name and an address":
        "Zakat contribution, branch counter",
    "Clean memo — the encoder correctly finds nothing":
        "Kiraya jama karwa diya",
}

NAVY, SLATE, COBALT, PAPER = "#0F172A", "#1E293B", "#2563EB", "#F8FAFC"
EMERALD = scan.HIGHLIGHT

LOGO = """
<svg viewBox="0 0 44 52" width="42" height="50" aria-label="PipelineGuard"
     xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="pgFace" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#24354f"/>
      <stop offset="100%" stop-color="#0d1626"/>
    </linearGradient>
    <linearGradient id="pgEdge" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#34d399"/>
      <stop offset="100%" stop-color="#059669"/>
    </linearGradient>
  </defs>
  <path d="M22 1.5 42 8.4v15.9c0 12.4-8.2 21.6-20 25.9C10.2 45.9 2 36.7 2 24.3V8.4z"
        fill="url(#pgFace)" stroke="url(#pgEdge)" stroke-width="2.6"
        stroke-linejoin="round"/>
  <path d="M22 6 37.5 11.3v12.9c0 9.9-6.4 17.4-15.5 21-9.1-3.6-15.5-11.1-15.5-21V11.3z"
        fill="none" stroke="#10B981" stroke-opacity=".33" stroke-width="1.1"/>
  <path d="M22 1.5 42 8.4v15.9c0 3-.5 5.8-1.4 8.4L22 1.5z"
        fill="#F8FAFC" fill-opacity=".05"/>
  <text x="22" y="34" text-anchor="middle" fill="#10B981"
        font-family="Georgia, 'Times New Roman', serif" font-size="25"
        font-weight="700">P</text>
</svg>
"""

# Resolved from __file__, not the working directory: Streamlit is launched from
# the repo root, but nothing enforces that and a missing icon is silent.
FAVICON = Path(__file__).resolve().parent / "assets" / "shield.png"

st.set_page_config(page_title="PipelineGuard",
                   page_icon=str(FAVICON) if FAVICON.exists() else "🛡️",
                   layout="wide", initial_sidebar_state="expanded")

# Sticky tabs, the loading overlay, and the logo lockup. Streamlit ships no
# primitive for a dismissible overlay, so it is a checkbox and a full-page
# label -- no script tag, which unsafe_allow_html strips anyway.
st.markdown(f"""<style>
/* The section bar follows the page.
   Sticky goes on the stLayoutWrapper, not on st-key-pg_nav itself: Streamlit
   wraps a keyed container in a wrapper sized exactly to its content, so a
   sticky child has no room to travel and never sticks.
   top is 3.75rem because stHeader is 60px tall at z-index 999990 -- a bar
   stuck to the viewport top parks underneath it and looks like it vanished.
   The side box-shadows paint over the block container's horizontal padding so
   content cannot scroll behind the bar's edges. */
[data-testid="stLayoutWrapper"]:has(> .st-key-pg_nav) {{
  position: sticky; top: 3.75rem; z-index: 900;
  background: {NAVY}; padding: .5rem 0;
  border-bottom: 1px solid rgba(248,250,252,.14);
  box-shadow: -140px 0 0 0 {NAVY}, 140px 0 0 0 {NAVY},
              0 8px 18px -14px rgba(0,0,0,.95);
}}
.st-key-pg_nav [data-testid="stSegmentedControl"] button {{
  font-weight: 600; letter-spacing: .01em;
}}

.pg-lockup {{ display:flex; align-items:center; gap:.7rem; margin:.2rem 0 .1rem; }}
.pg-word {{ font-size:1.42rem; font-weight:800; color:{PAPER}; line-height:1.1; }}
.pg-tag {{ color:#94a3b8; font-size:.86rem; margin:.1rem 0 .9rem; }}

/* The welcome lockup. Sized off the sidebar one so the logo reads as the same
   mark at both scales rather than a second, larger badge. */
.pg-hero {{ display:flex; align-items:center; gap:1.1rem; margin:1.2rem 0 .4rem; }}
/* height only: the mark is 44x52, so forcing a square would squash it. */
.pg-hero svg {{ width:auto; height:72px; }}
.pg-hero h1 {{ font-size:2.1rem; font-weight:800; color:{PAPER};
               margin:0; line-height:1.1; }}
.pg-hero p {{ color:#94a3b8; font-size:1rem; margin:.25rem 0 0; }}

/* Metric cards. Bare numbers float against the page background and read as
   unrelated to each other; a slate panel groups them and separates the row
   from the tables underneath. */
[data-testid="stMetric"] {{
  background: {SLATE};
  border: 1px solid rgba(248,250,252,.09);
  border-radius: 10px;
  padding: .8rem 1rem .9rem;
  box-shadow: 0 8px 18px -14px rgba(0,0,0,.95);
}}
[data-testid="stMetricLabel"] p {{ color:#94a3b8; font-size:.82rem; }}

.pg-dismiss {{ display:none; }}
.pg-overlay {{
  position: fixed; inset: 0; z-index: 2000; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  background: rgba(15,23,42,.72); backdrop-filter: blur(2px);
}}
.pg-dismiss:checked + .pg-overlay {{ display: none; }}
.pg-card {{
  display:flex; flex-direction:column; align-items:center; gap:.55rem;
  background:{SLATE}; border:1px solid rgba(16,185,129,.3);
  border-radius:14px; padding:1.6rem 2.2rem;
  box-shadow:0 20px 50px -20px #000;
}}
.pg-ring {{
  width:52px; height:52px; border-radius:50%;
  border:4px solid rgba(248,250,252,.14); border-top-color:{EMERALD};
  animation: pg-spin .9s linear infinite;
}}
.pg-ring-sm {{ width:16px; height:16px; border-width:2px; }}
@keyframes pg-spin {{ to {{ transform: rotate(360deg); }} }}
.pg-msg {{ color:{PAPER}; font-weight:600; }}
.pg-sub {{ color:#94a3b8; font-size:.82rem; }}
.pg-hint {{ color:#64748b; font-size:.72rem; margin-top:.3rem; }}
.pg-pill {{
  position: fixed; top: 3.4rem; right: 1rem; z-index: 2001;
  display:flex; align-items:center; gap:.5rem;
  background:{SLATE}; color:{PAPER}; font-size:.8rem; font-weight:600;
  border:1px solid rgba(16,185,129,.35); border-radius:999px;
  padding:.4rem .85rem; box-shadow:0 10px 24px -14px #000;
}}
</style>""", unsafe_allow_html=True)


def loading(placeholder, message: str, detail: str = "") -> None:
    """Centre-screen spinner plus a corner pill that outlives dismissing it.

    The overlay is click-to-dismiss so a long scan does not trap the page; the
    pill keeps reporting until the caller clears the placeholder.
    """
    placeholder.markdown(
        f'<input type="checkbox" id="pg-dismiss" class="pg-dismiss">'
        f'<label for="pg-dismiss" class="pg-overlay"><span class="pg-card">'
        f'<span class="pg-ring"></span>'
        f'<span class="pg-msg">{message}</span>'
        f'<span class="pg-sub">{detail}</span>'
        f'<span class="pg-hint">click anywhere to dismiss — this keeps running</span>'
        f'</span></label>'
        f'<div class="pg-pill"><span class="pg-ring pg-ring-sm"></span>'
        f'<span>{message}</span></div>',
        unsafe_allow_html=True,
    )


@st.cache_resource(show_spinner=False)
def load_tier1() -> RulesDetector:
    return RulesDetector()


@st.cache_resource(show_spinner=False)
def encoder_slot(model: str, revision: str, device: str,
                 variant: str | None) -> dict:
    """One mutable holder shared by every session and by the loader thread.

    cache_resource rather than a module global: Streamlit re-executes this file
    top to bottom on each rerun, so a global would be rebuilt every time. Keyed
    on the pin, so changing it builds a new slot instead of serving old weights.
    """
    return {"state": "idle", "detector": None, "error": None,
            "lock": threading.Lock()}


def start_encoder(slot: dict, model: str, revision: str, device: str,
                  variant: str | None) -> None:
    """Kick off the ~20 s load off the main thread, so Tier 1 stays usable.

    The worker must never touch `st`. Streamlit binds a script context to the
    thread that runs the page, and calls from anywhere else either warn or do
    nothing. So the thread only writes into `slot`, and the page reads it.
    """
    with slot["lock"]:
        if slot["state"] != "idle":
            return
        slot["state"] = "loading"

    def work() -> None:
        try:
            from pipelineguard.detectors.tier2_encoder import Tier2Detector

            detector = Tier2Detector(model, threshold=settings.tier2_threshold,
                                     device=device,
                                     batch_size=settings.tier2_batch_size,
                                     revision=revision, variant=variant)
            detector.load()
            slot["detector"] = detector
            slot["state"] = "ready"
        except Exception as exc:  # noqa: BLE001 - shown on the page, not swallowed
            slot["error"] = f"{type(exc).__name__}: {exc}"
            slot["state"] = "error"

    threading.Thread(target=work, name="pg-encoder-load", daemon=True).start()


@st.fragment(run_every=1.0)
def encoder_heartbeat() -> None:
    """Streamlit cannot notice a thread finishing, so poll until it has.

    Only rendered while the state is 'loading', so the polling stops by itself
    once the app rerun below sees a settled state.
    """
    slot = encoder_slot(settings.tier2_model, settings.tier2_model_revision,
                        settings.tier2_device, settings.tier2_variant)
    if slot["state"] != "loading":
        st.rerun(scope="app")


def tier_name(tier: Tier) -> str:
    return TIER_LABEL.get(tier, f"Tier {int(tier)}")


def category_of(entity_type: str) -> str:
    classification = compliance.classify(entity_type)
    return classification.data_category if classification else "unclassified"


def findings_table(result: scan.ScanResult) -> list[dict]:
    """One row per finding, with the matched text sliced from the input.

    `Finding` deliberately stores no value, so the text is recovered here from
    the span. That is a property of this screen, not of the audit trail.
    """
    return [
        {
            "entity": f.entity_type,
            "matched": result.text[f.span_start:f.span_end],
            "caught by": tier_name(f.tier),
            "confidence": f"{f.confidence:.2f}",
            "span": f"[{f.span_start}, {f.span_end})",
            "category": category_of(f.entity_type),
        }
        for f in result.findings
    ]


def show_result(result: scan.ScanResult) -> None:
    """The shared original/redacted/trace view used by the playground."""
    if result.truncated:
        st.warning(
            f"Input was longer than {scan.MAX_CHARS:,} characters and was cut "
            "before scanning. Text past the cut was not examined."
        )

    left, right = st.columns(2)
    with left:
        st.caption("Original — highlighted where the pipeline would redact")
        st.markdown(scan.highlight_html(result.text, result.spans),
                    unsafe_allow_html=True)
    with right:
        st.caption("Redacted — what a consumer of txn.clean would receive")
        st.code(result.redacted, language=None, wrap_lines=True)

    if not result.findings:
        st.info("Nothing detected.")
        return

    a, b, c = st.columns(3)
    a.metric("Findings", len(result.findings))
    b.metric("Spans after merge", len(result.spans))
    c.metric("Identifying characters redacted",
             f"{result.coverage:.0%}",
             help=f"{result.masked_chars} of {result.identifying_chars} "
                  "alphanumeric characters. Punctuation carries no identity, "
                  "so it is excluded from both sides.")

    st.caption("Findings before spans are merged")
    st.dataframe(findings_table(result), width="stretch", hide_index=True)

    if len(result.spans) != len(result.findings):
        st.caption(
            "Overlapping findings are unioned before the rewrite, so a merged "
            "span can carry two entity types. The findings themselves stay "
            "separate for the audit."
        )


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown(
        f'<div class="pg-lockup">{LOGO}<span class="pg-word">PipelineGuard</span></div>'
        '<div class="pg-tag">Protect your data, safely and securely</div>',
        unsafe_allow_html=True,
    )

    st.subheader("Detection")

    # Resolved before the widgets, because every Tier 2 control below is
    # disabled until the encoder can actually answer. A live slider over a
    # model that is still loading promises something the app cannot do.
    tier1 = load_tier1()
    tier2 = None
    slot = encoder_slot(settings.tier2_model, settings.tier2_model_revision,
                        settings.tier2_device, settings.tier2_variant)

    use_tier2 = st.toggle(
        "Tier 2 encoder", value=True,
        help="Names and addresses have no fixed format, so only the encoder "
             "finds them. Off, you see exactly what the processor does with "
             "TIER2_ENABLED=false.",
    )
    if use_tier2:
        start_encoder(slot, settings.tier2_model,
                      settings.tier2_model_revision, settings.tier2_device,
                      settings.tier2_variant)
    encoder_ready = use_tier2 and slot["state"] == "ready"
    if encoder_ready:
        tier2 = slot["detector"]

    st.markdown("**Tier 1 — rules**")
    st.caption("Deterministic, predictable and runs in microseconds.")
    selected = {t for t in RULE_TYPES if st.checkbox(t, value=True, key=f"t1_{t}")}

    st.markdown("**Tier 2 — encoder**")
    st.caption("Reads meaning, not patterns. Catches names and addresses "
               "no rule can match.")
    for entity_type in ENCODER_TYPES:
        if st.checkbox(entity_type, value=True, key=f"t2_{entity_type}",
                       disabled=not encoder_ready):
            if encoder_ready:
                selected.add(entity_type)

    if DEMO:
        # Dropped on the demo build, and not merely hidden: `tier2_settings`
        # only takes its global lock when a setting is overridden, so leaving
        # the threshold alone lets visitors' scans run without queueing behind
        # each other.
        threshold = None
        st.caption(f"Confidence threshold fixed at {settings.tier2_threshold} "
                   "— the value measured against this checkpoint.")
    else:
        threshold = st.slider(
            "Confidence threshold", min_value=0.10, max_value=0.95,
            value=float(settings.tier2_threshold), step=0.05,
            disabled=not encoder_ready,
            help="Model and threshold are a pair. 0.55 is the value measured "
                 "against this checkpoint; another checkpoint would need its own.",
        )
    if DEMO:
        # Same reason as the threshold above: any override takes the lock. The
        # detector already defaults to True, which is the shipped behaviour.
        extend = None
    else:
        extend = st.checkbox(
            "Widen and bridge address spans", value=True,
            disabled=not encoder_ready,
            help="Walks outward over a leading house or plot number and a "
                 "trailing city, then joins address spans separated by a gap. "
                 "Turn it off to see the raw model span.",
        )

    if use_tier2:
        if slot["state"] == "ready":
            st.success("Encoder ready", icon=":material/check_circle:")
        elif slot["state"] == "error":
            st.error(f"Encoder failed to load: {slot['error']}")
            st.caption("Tier 2 needs torch and gliner: `pip install '.[tier2]'`.")
        else:
            st.info("Encoder loading… rules work now.",
                    icon=":material/hourglass_top:")
            encoder_heartbeat()

    with st.expander("Model provenance"):
        if tier2 is None:
            st.write("Encoder not loaded.")
        else:
            offline = os.getenv("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true"}
            st.write(f"**Checkpoint** `{tier2.model_id}`")
            st.write(f"**Revision** `{(tier2.resolved_revision() or 'main')[:12]}`")
            st.write(f"**Precision** `{tier2.variant or 'fp32'}`")
            st.write(f"**Device** `{tier2.device}`")
            st.write(f"**Offline** `{offline}`")
            try:
                from pipelineguard.detectors.tier2_encoder import cached_base_revisions
                cached = cached_base_revisions(settings.tier2_base_model)
            except Exception:  # noqa: BLE001
                cached = []
            # Only the failure states are worth screen space. A correct pin is
            # the expected condition and does not need announcing.
            if not offline:
                st.warning("HF_HUB_OFFLINE is unset, so the backbone config "
                           "resolves at `main`. Run `python -m "
                           "pipelineguard.prefetch`, then set it.")
            elif cached != [settings.tier2_base_revision]:
                st.warning(f"Backbone cache holds {[r[:12] for r in cached]}; "
                           f"expected {settings.tier2_base_revision[:12]}.")

    st.divider()
    # "This machine" is the visitor's laptop locally and a rented container on
    # the demo, and only one of those is a privacy claim the visitor can check.
    # decisions.md section 1 accepts that boundary; it must not be blurred here.
    st.caption(
        "Read in memory on the server, scanned, and dropped when the scan "
        "finishes. Nothing is written to disk, logged, or added to the audit "
        "database — but the server is shared free hosting we do not own."
        if DEMO else
        "Runs entirely on this machine. Nothing you type or upload leaves it, "
        "and nothing is written to the audit database from here."
    )


active_types = selected or None
scan_kwargs = dict(tier1=tier1, tier2=tier2, entity_types=active_types,
                   threshold=threshold if tier2 else None,
                   extend_addresses=extend if tier2 else None)


# --------------------------------------------------------------------------- #
# Welcome
# --------------------------------------------------------------------------- #
# Shown first so the encoder has something to load behind. The rules are ready
# by now either way -- it is only the encoder that costs ~20 s.
if not st.session_state.get("pg_started"):
    st.markdown(
        f'<div class="pg-hero">{LOGO}'
        f'<div><h1>PipelineGuard</h1>'
        f'<p>Protect your data, safely and securely</p></div></div>',
        unsafe_allow_html=True,
    )
    st.write("")

    one, two, three = st.columns(3)
    one.markdown("**Playground**")
    one.caption("Paste a memo. See what each tier finds, and what the "
                "pipeline would write downstream.")
    two.markdown("**Batch scan**")
    two.caption(f"Scan a CSV or text file, up to {MAX_ROWS} rows, and export "
                "a redaction report.")
    three.markdown("**Governance report**")
    three.caption("Read the audit trail the running pipeline writes to "
                  "Postgres.")

    st.write("")
    ready = slot["state"] == "ready" or not use_tier2
    left, right = st.columns([2, 3])
    if left.button("Start", type="primary", width="stretch"):
        st.session_state["pg_started"] = True
        st.rerun()
    if not use_tier2:
        right.caption("Tier 2 is off. Rules only, which is what the processor "
                      "does with `TIER2_ENABLED=false`.")
    elif ready:
        right.caption("Both tiers ready.")
    elif slot["state"] == "error":
        right.caption("Rules are ready. The encoder failed — see the sidebar.")
    else:
        right.caption("Rules are ready now. The encoder is still loading; "
                      "start anyway and it will switch on by itself.")

    if DEMO:
        st.write("")
        st.info(PRIVACY_NOTICE, icon=":material/lock:")
    else:
        st.caption(
            "Runs entirely on this machine. Nothing you type or upload leaves it."
        )
    st.stop()


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
SECTIONS = ["Playground", "Batch scan", "Governance report"]
SECTION_ICONS = ["terminal", "database", "shield-check"]

# Keyed, so the selection lives in session state. st.tabs keeps its selection
# client-side and springs back to the first tab on a rerun, which is what made
# "Load report" build a report the reader never saw.
nav = st.container(key="pg_nav")
with nav:
    section = option_menu(
        None, SECTIONS, icons=SECTION_ICONS, default_index=0,
        orientation="horizontal", key="pg_section",
        styles={
            # Centred and content-width. Left to itself the component spreads
            # each item across a third of the page, which turns the active tab
            # into a banner rather than a tab.
            "container": {"padding": "0", "background-color": NAVY,
                          "max-width": "680px", "margin": "0 auto",
                          "justify-content": "center"},
            "icon": {"font-size": "0.95rem", "margin-right": ".4rem"},
            "nav-link": {
                "font-size": "0.92rem", "font-weight": "600",
                "color": "#94a3b8", "padding": ".5rem 1.15rem",
                "margin": "0 .3rem", "border-radius": "8px",
                "flex": "none", "white-space": "nowrap",
                "--hover-color": SLATE,
            },
            "nav-link-selected": {"background-color": COBALT, "color": PAPER},
        },
    ) or SECTIONS[0]

if section == "Playground":
    st.subheader("Scan one memo")
    choice = st.selectbox("Start from a documented case", list(PRESETS), index=0)

    with st.form("playground_form", clear_on_submit=False):
        text = st.text_area("Text to scan", value=PRESETS[choice], height=140,
                            key=f"text_{choice}")
        submitted = st.form_submit_button("Scan text", type="primary",
                                          width="stretch")

    if submitted:
        st.session_state["playground_text"] = text

    pending = st.session_state.get("playground_text", PRESETS[choice])
    if not pending.strip():
        st.info("Type something, or pick a case above, then press Scan text.")
    else:
        show_result(scan.scan_text(pending, **scan_kwargs))
        if not use_tier2:
            st.caption(
                "Tier 1 matches formats — CNIC, IBAN, phone, email. Names and "
                "addresses have no format; turn on the encoder to catch them."
            )

elif section == "Batch scan":
    st.subheader("Scan a file")
    if DEMO:
        st.caption(
            f"Up to {MAX_ROWS:,} rows. On this shared machine the encoder runs "
            "at about 225 ms per row, so a full batch takes roughly 11 seconds."
        )
        st.info(PRIVACY_NOTICE, icon=":material/lock:")
    else:
        st.caption(
            f"Up to {MAX_ROWS:,} rows. The encoder runs at about 16 ms per row "
            "on the reference GPU, so a full batch takes roughly 8 seconds."
        )
    upload = st.file_uploader("CSV or TXT", type=["csv", "txt"])

    column = None
    if upload is not None and upload.name.lower().endswith(".csv"):
        columns = scan.csv_columns(upload.getvalue())
        if columns:
            widest = scan.widest_column(upload.getvalue())
            column = st.selectbox(
                "Column to scan", columns,
                index=columns.index(widest) if widest in columns else 0,
                help="Defaults to the column with the most text, which is "
                     "usually the free-text field rather than an id.")

    if upload is not None and st.button("Scan file", type="primary"):
        try:
            rows = scan.read_rows(upload.getvalue(), upload.name, column)
        except ValueError as exc:
            st.error(str(exc))
            rows = []

        if len(rows) > MAX_ROWS:
            st.warning(f"File has {len(rows):,} rows; scanning the first "
                       f"{MAX_ROWS:,}.")
            rows = rows[:MAX_ROWS]

        if rows:
            spinner = st.empty()
            loading(spinner, "Scanning…", f"{len(rows):,} rows")
            started = datetime.now(timezone.utc)
            results = scan.scan_batch(
                rows,
                progress=lambda done, total: loading(
                    spinner, f"Scanning… {done * 100 // total}%",
                    f"{done:,} of {total:,} rows"),
                **scan_kwargs,
            )
            finished = datetime.now(timezone.utc)
            spinner.empty()

            data = batch_report.build_report_data(
                results, source_name=upload.name,
                started_at=started, finished_at=finished)
            markdown = batch_report.build_markdown(
                data, results, source_name=upload.name,
                tier2_used=tier2 is not None,
                threshold=threshold if tier2 else None,
                entity_types=sorted(active_types) if active_types else None)
            # The scanned rows are dropped here, not held for the session.
            # Everything below reads counts and redacted output, both already
            # computed, so nothing needs the original again.
            st.session_state["batch"] = ([r.without_text() for r in results],
                                         data, markdown, upload.name)

    if "batch" in st.session_state:
        results, data, markdown, name = st.session_state["batch"]
        counts = dict(data.disposition)
        identifying = sum(r.identifying_chars for r in results)
        masked = sum(r.masked_chars for r in results)

        a, b, c, d = st.columns(4)
        a.metric("Rows scanned", f"{len(results):,}")
        b.metric("Redacted", f"{counts.get('redacted', 0):,}")
        c.metric("Quarantined", f"{counts.get('quarantined', 0):,}")
        d.metric("Identifying characters redacted",
                 f"{masked / identifying:.0%}" if identifying else "--")

        if data.entities:
            st.caption("Personal data observed")
            st.dataframe(
                [{"entity": e.entity_type,
                  "category": category_of(e.entity_type),
                  "mentions": e.mentions,
                  "rows": e.messages,
                  "lowest confidence": f"{e.min_confidence:.2f}"}
                 for e in data.entities],
                width="stretch", hide_index=True)

        st.caption("Redacted output — the original text is never shown here")
        st.dataframe(
            [{"row": i,
              "outcome": batch_report.disposition_of(r.findings),
              "redacted text": r.redacted}
             for i, r in enumerate(results)],
            width="stretch", hide_index=True)

        if any(f.entity_type == "ADDRESS" for r in results for f in r.findings):
            st.warning(
                "Address redaction is routinely incomplete rather than binary, "
                "so a fragment may remain in a row that is otherwise masked. "
                "Treat these rows as reviewable output, not as cleared data."
            )

        stem = name.rsplit(".", 1)[0]
        st.caption(
            "Two documents from the same scan. The redaction report is the "
            "technical artefact and lists every row. The executive summary is "
            "the governance view of the same figures, written for a reader who "
            "needs the numbers first and the statute last."
        )
        left, right = st.columns(2)
        left.download_button("Download Redaction Report (Markdown)", markdown,
                             file_name=f"{stem}-redaction-report.md",
                             mime="text/markdown", width="stretch")
        try:
            pdf = batch_report.markdown_to_pdf(markdown, title="Redaction Report")
            right.download_button("Download Redaction Report (PDF)", pdf,
                                  file_name=f"{stem}-redaction-report.pdf",
                                  mime="application/pdf", width="stretch")
        except ImportError:
            right.info("PDF export needs `pip install '.[ui]'`.")

        summary = report.render_summary(data)
        left, right = st.columns(2)
        left.download_button("Download Executive Summary (Markdown)", summary,
                             file_name=f"{stem}-executive-summary.md",
                             mime="text/markdown", width="stretch")
        try:
            right.download_button(
                "Download Executive Summary (PDF)",
                batch_report.markdown_to_pdf(
                    summary, title="Data Governance Summary",
                    orientation="portrait"),
                file_name=f"{stem}-executive-summary.pdf",
                mime="application/pdf", width="stretch")
        except ImportError:
            right.info("PDF export needs `pip install '.[ui]'`.")

elif section == "Governance report" and DEMO:
    # No broker and no database on the demo, so there is no live audit trail to
    # read. Showing a stored run is honest; a Connect button that always fails
    # would teach a visitor nothing.
    st.subheader("Example governance report")
    st.caption(
        "Normally this reads the Postgres audit trail the running pipeline "
        "writes. The demo has no pipeline behind it, so this is a stored run "
        f"over {SAMPLE_RECORDS} synthetic records — real output, not a mockup."
    )
    st.info("This is not your scan. For a report on the file you uploaded, "
            "use **Batch scan**.", icon=":material/info:")
    sample = SAMPLE_SUMMARY.read_text(encoding="utf-8") if SAMPLE_SUMMARY.exists() else ""
    if not sample:
        st.warning("The stored example is missing from this build.")
    else:
        # Stamped before it is handed to the download buttons, not only on the
        # page: a file that leaves the browser must carry its own provenance,
        # because whoever opens it later cannot see this caption.
        stamped = report.stamp_example(sample)
        left, right = st.columns(2)
        left.download_button("Download example (Markdown)", stamped,
                             file_name="governance-summary-example.md",
                             mime="text/markdown", width="stretch")
        try:
            right.download_button(
                "Download example (PDF)",
                batch_report.markdown_to_pdf(
                    stamped, title="Data Governance Summary (example)",
                    orientation="portrait"),
                file_name="governance-summary-example.pdf",
                mime="application/pdf", width="stretch")
        except ImportError:
            right.info("PDF export needs `pip install '.[ui]'`.")
        st.markdown(stamped.replace(report.PAGE_BREAK, "---"))

elif section == "Governance report":
    st.subheader("Governance report from the audit trail")
    st.caption(
        "Reads the Postgres audit trail the running pipeline writes. This is "
        "the summary view, written for a reader who needs the numbers rather "
        "than the internals. `python -m pipelineguard.report` still writes the "
        "full technical version."
    )

    left, mid, right = st.columns(3)
    since_on = left.checkbox("Limit start", value=False)
    since_date = left.date_input(
        "From", value=(datetime.now(timezone.utc) - timedelta(days=1)).date(),
        disabled=not since_on)
    until_on = mid.checkbox("Limit end", value=False)
    until_date = mid.date_input("Until", value=datetime.now(timezone.utc).date(),
                                disabled=not until_on)
    max_queue = right.number_input("Max review items", 1, 500, 50)

    if st.button("Load report", type="primary"):
        since = (datetime.combine(since_date, time.min, timezone.utc)
                 if since_on else None)
        until = (datetime.combine(until_date, time.min, timezone.utc)
                 if until_on else None)
        spinner = st.empty()
        loading(spinner, "Reading the audit trail…", settings.postgres_dsn)
        try:
            import psycopg

            conn = psycopg.connect(settings.postgres_dsn,
                                   connect_timeout=DB_TIMEOUT_S)
            try:
                data = report.fetch(conn, since, until, int(max_queue))
            finally:
                conn.close()
            # Kept in state, not rendered here: a button is True for exactly one
            # run, so anything drawn inside this block vanishes on the next one.
            st.session_state["governance"] = ("ok", report.render_summary(data))
        except Exception as exc:  # noqa: BLE001 - the cause is what matters
            st.session_state["governance"] = ("error", str(exc))
        finally:
            spinner.empty()

    outcome = st.session_state.get("governance")
    if outcome and outcome[0] == "error":
        st.error(f"Could not read the audit trail: {outcome[1]}")
        st.caption(
            "The stack must be up and the schema applied: "
            "`docker compose up -d postgres` creates it from `db/init.sql`. "
            f"Connecting to `{settings.postgres_dsn}`."
        )
    elif outcome:
        left, right = st.columns(2)
        left.download_button("Download report (Markdown)", outcome[1],
                             file_name="governance-report.md",
                             mime="text/markdown", width="stretch")
        try:
            right.download_button(
                "Download report (PDF)",
                batch_report.markdown_to_pdf(
                    outcome[1], title="Data Governance Summary",
                    orientation="portrait"),
                file_name="governance-report.pdf",
                mime="application/pdf", width="stretch")
        except ImportError:
            right.info("PDF export needs `pip install '.[ui]'`.")
        st.markdown(outcome[1].replace(report.PAGE_BREAK, "---"))

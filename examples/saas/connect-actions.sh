#!/bin/sh
# Wires the demo rules to Slack and Zendesk, through the same API the
# Integrations and Rules screens use.
#
#   sh examples/saas/connect-actions.sh
#
# Every rule starts in shadow, so nothing is sent until you promote one under
# Activity.
#
# With no credentials exported, the rules are pointed at the Demo inbox
# instead: promote one and its message appears on the Demo inbox page, with
# nothing sent anywhere. Slack and Zendesk are still added (with placeholder
# credentials) so the Integrations page shows them. Export real ones to point
# the rules at the real systems:
#
#   SLACK_BOT_TOKEN      xoxb-...
#   ZENDESK_SUBDOMAIN    acme  (for https://acme.zendesk.com)
#   ZENDESK_EMAIL        you@company.com
#   ZENDESK_API_TOKEN    ...
set -eu

# Decided before the placeholders are filled in below: only credentials you
# exported count as real.
if [ -n "${SLACK_BOT_TOKEN:-}" ]; then
  CHAT='"integration":"Acme Slack","action":"send_message"'; CHAT_TO="Slack"
else
  CHAT='"integration":"Demo inbox","action":"post_message"'; CHAT_TO="Demo inbox"
fi
if [ -n "${ZENDESK_API_TOKEN:-}" ]; then
  TICKET='"integration":"Acme Zendesk","action":"create_ticket"'; TICKET_TO="Zendesk"
else
  TICKET='"integration":"Demo inbox","action":"create_ticket"'; TICKET_TO="Demo inbox"
fi

API="http://127.0.0.1:${ROWFIRE_UI_PORT:-8000}/api"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-xoxb-demo-not-a-real-token}"
ZENDESK_SUBDOMAIN="${ZENDESK_SUBDOMAIN:-acme}"
ZENDESK_EMAIL="${ZENDESK_EMAIL:-support-bot@acme.example}"
ZENDESK_API_TOKEN="${ZENDESK_API_TOKEN:-demo-not-a-real-token}"

post() {
  curl -fsS -X POST "$API/$1" -H 'content-type: application/json' -d "$2" >/dev/null
  echo "  ok  $1 $3"
}

echo "integrations"
post integrations "{\"name\":\"Acme Slack\",\"provider\":\"slack\",
  \"credentials\":{\"bot_token\":\"$SLACK_BOT_TOKEN\"}}" "Acme Slack"
post integrations "{\"name\":\"Acme Zendesk\",\"provider\":\"zendesk\",
  \"base_url\":\"https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2\",
  \"credentials\":{\"email\":\"$ZENDESK_EMAIL/token\",\"api_token\":\"$ZENDESK_API_TOKEN\"}}" "Acme Zendesk"

post integrations '{"name":"Demo inbox","provider":"demo_inbox"}' "Demo inbox"

echo "actions"
post bindings '{"rule_name":"announce_enterprise_signup",'"$CHAT"',
  "parameters":{"channel":"#sales",
    "text":":tada: New Enterprise account: *{{ account }}* ({{ domain }}) — {{ seats }} seats in {{ region }}. Owner: {{ owner_name }} <{{ owner_email }}>"}}' \
  "announce_enterprise_signup -> $CHAT_TO #sales"

post bindings '{"rule_name":"billing_heads_up",'"$CHAT"',
  "parameters":{"channel":"#billing",
    "text":":warning: {{ account }} ({{ plan }}) — charge for {{ invoice_no }} declined: {{ decline_code }}, attempt {{ attempt }}, ${{ amount }}"}}' \
  "billing_heads_up -> $CHAT_TO #billing"

post bindings '{"rule_name":"billing_ticket",'"$TICKET"',
  "parameters":{"subject":"Payment failed for {{ account }}",
    "body":"Hi {{ owner_name }},\n\nWe could not charge the card on file for invoice {{ invoice_no }} (${{ amount }}, {{ decline_code }}). Could you update your payment details?",
    "requester_email":"{{ owner_email }}","requester_name":"{{ owner_name }}",
    "priority":"high","tags":"[\"billing\", \"payment_failed\"]"},
  "recipient_template":"{{ account_id }}"}' \
  "billing_ticket -> $TICKET_TO ticket"

post bindings '{"rule_name":"rescue_stalled_trial",'"$TICKET"',
  "parameters":{"subject":"Trial for {{ account }} ends {{ trial_ends_at }} — not activated yet",
    "body":"{{ owner_name }} has not completed setup. Reach out and offer an onboarding call before the trial ends.",
    "requester_email":"{{ owner_email }}","requester_name":"{{ owner_name }}",
    "priority":"normal","tags":"[\"trial\", \"onboarding\"]"}}' \
  "rescue_stalled_trial -> $TICKET_TO ticket"

post bindings '{"rule_name":"detractor_follow_up",'"$TICKET"',
  "parameters":{"subject":"NPS {{ score }} from {{ account }}",
    "body":"{{ respondent }} scored us {{ score }}/10:\n\n> {{ comment }}",
    "requester_email":"{{ respondent }}","requester_name":"{{ respondent }}",
    "priority":"high","tags":"[\"nps\", \"detractor\"]"}}' \
  "detractor_follow_up -> $TICKET_TO ticket"

post bindings '{"rule_name":"detractor_to_slack",'"$CHAT"',
  "parameters":{"channel":"#customer-voice",
    "text":":speech_balloon: NPS {{ score }} from {{ account }} ({{ plan }}): \"{{ comment }}\""}}' \
  "detractor_to_slack -> $CHAT_TO #customer-voice"

post bindings '{"rule_name":"escalate_unanswered_urgent",'"$CHAT"',
  "parameters":{"channel":"#support",
    "text":":rotating_light: Urgent ticket #{{ id }} has no reply yet: \"{{ subject }}\" from {{ requester_email }} via {{ channel }}"}}' \
  "escalate_unanswered_urgent -> $CHAT_TO #support"

package magertron

# Magertron™ has already allowed the call; these are business rules on top.
# Every rule adds a reason to `deny`; the decision is assembled once at the
# bottom, so several rules can fire on the same call without a conflict.
#
# The numbers live in the data file (install.sh --opa-data), not here:
#   data.allowances.default_usd        OPTIONAL: everyone else's allowance
#   data.allowances.people[<person>]   a person's own allowance (overrides)
#   data.department_budgets_usd[<dept>] a department's monthly budget
# Change caps by swapping the JSON — this file does not change.

default decision := {"allow": true}

decision := {"allow": false, "reasons": sort([m | some m in deny])} if count(deny) > 0

# Lab rule: no quotes for TSLA.
deny contains sprintf("quotes for %v are not permitted by company policy", [input.arguments.symbol]) if {
	input.tool == "GLOBAL_QUOTE"
	input.arguments.symbol == "TSLA"
}

# Who the spend belongs to: the human on a delegated call, otherwise the caller.
person := input.on_behalf_of if input.on_behalf_of != ""
person := input.subject if input.on_behalf_of == ""

# A person's allowance: their own entry; else default_usd IF the data file sets
# one. With no default, only the people listed are capped — everyone else
# (service accounts, the regression suite) is untouched.
allowance_usd := data.allowances.people[person]

allowance_usd := data.allowances.default_usd if {
	not data.allowances.people[person]
}

deny contains sprintf("monthly AI allowance of $%v reached: $%.2f spent in %v", [allowance_usd, input.spend.mtd_usd, input.spend.period]) if {
	input.spend.source == "cache"
	input.spend.mtd_usd >= allowance_usd
}

# Department budget, only for departments listed in the data file.
deny contains sprintf("%v's monthly AI budget of $%v is used up: $%.2f spent", [dept, budget, input.spend.department.mtd_usd]) if {
	input.spend.source == "cache"
	dept := input.spend.department.name
	budget := data.department_budgets_usd[dept]
	input.spend.department.mtd_usd >= budget
}

# Netops: which TOPIC, not just which tool. Magertron's RBAC already decides
# who may call the Kafka tools; this decides who may use the command topic.
# Only the operator role may read or write network.commands - so a diagnostic
# agent can watch network.events but can never see, or forge, a fix.
kafka_topic_tools := {"subscribe_topic", "consume_topic", "produce_to_topic", "register_webhook"}
deny contains sprintf("only netops-operator may use %v; %v is not an operator", [input.arguments.topic, input.subject]) if {
	kafka_topic_tools[input.tool]
	input.arguments.topic == "network.commands"
	not "custom:netops-operator" in input.roles
}

# ...and the PERSON: on a delegated call, the human the agent acts for must be an
# operator too. Otherwise an operator agent could publish a fix for anyone, and
# taking the role away from the person would change nothing.
# input.on_behalf_of_roles = the person's current roles (Magertron 4.1.98+).
deny contains sprintf("only netops-operator may use %v; %v is acting for %v, who is not an operator", [input.arguments.topic, input.subject, input.on_behalf_of]) if {
	kafka_topic_tools[input.tool]
	input.arguments.topic == "network.commands"
	input.on_behalf_of != ""
	not "custom:netops-operator" in object.get(input, "on_behalf_of_roles", [])
}

# PII on LLM prompts and replies: the action per person or agent, from the data
# file (data.pii_actions[<person>] = "pass" | "redact" | "block"). Asked only
# when the server's PII setting is Detect, Redact or Block and something was
# found. Nobody listed -> the server's setting decides. A deny still wins.
pii_action := data.pii_actions[person] if {
	input.server_type == "llm"
	input.arguments.pii
	data.pii_actions[person] in {"pass", "redact", "block"}
}

decision := {"allow": true, "pii_action": pii_action} if {
	count(deny) == 0
	pii_action
}

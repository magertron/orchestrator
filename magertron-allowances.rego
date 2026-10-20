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

# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, today

from hrms.hr.doctype.full_and_final_statement.full_and_final_statement_loan_utils import (
	cancel_loan_repayment,
	process_loan_accrual,
)


class FullandFinalStatement(Document):
	def before_insert(self):
		self.get_outstanding_statements()

	def validate(self):
		self.validate_relieving_date()
		self.get_assets_statements()
		self.set_total_asset_recovery_cost()
		self.set_totals()

	def before_submit(self):
		self.validate_settlement("payables")
		self.validate_settlement("receivables")
		self.validate_assets()

	def on_submit(self):
		process_loan_accrual(self)

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry",)
		cancel_loan_repayment(self)

	def validate_relieving_date(self):
		if not self.relieving_date:
			frappe.throw(
				_("Please set {0} for Employee {1}").format(
					frappe.bold(_("Relieving Date")),
					get_link_to_form("Employee", self.employee),
				),
				title=_("Missing Relieving Date"),
			)

	def validate_settlement(self, component_type):
		for data in self.get(component_type, []):
			if data.status == "Unsettled":
				frappe.throw(
					_("Settle all Payables and Receivables before submission"),
					title=_("Unsettled Transactions"),
				)

	def validate_assets(self):
		pending_returns = []

		for data in self.assets_allocated:
			if data.action == "Return":
				if data.status == "Owned":
					pending_returns.append(_("Row {0}: {1}").format(data.idx, frappe.bold(data.asset_name)))
			elif data.action == "Recover Cost":
				data.status = "Owned"

		if pending_returns:
			msg = _("All allocated assets should be returned before submission")
			msg += "<br><br>"
			msg += ", ".join(d for d in pending_returns)
			frappe.throw(msg, title=_("Pending Asset Returns"))

	@frappe.whitelist()
	def get_outstanding_statements(self):
		if not self.relieving_date:
			frappe.throw(
				_("Set Relieving Date for Employee: {0}").format(get_link_to_form("Employee", self.employee))
			)

		if not self.payables:
			self.add_withheld_salary_slips()
			components = self.get_payable_component()
			self.create_component_row(components, "payables")
		if not self.receivables:
			components = self.get_receivable_component()
			self.create_component_row(components, "receivables")
		self.get_assets_statements()

	def get_assets_statements(self):
		if not len(self.get("assets_allocated", [])):
			for data in self.get_assets_movement():
				self.append("assets_allocated", data)

	def set_total_asset_recovery_cost(self):
		total_cost = 0
		for data in self.assets_allocated:
			if data.action == "Recover Cost":
				if not data.description:
					data.description = _("Asset Recovery Cost for {0}: {1}").format(
						data.reference, data.asset_name
					)
				total_cost += flt(data.cost)

		self.total_asset_recovery_cost = flt(total_cost, self.precision("total_asset_recovery_cost"))

	def set_totals(self):
		total_payable = sum(flt(row.amount) for row in self.payables)
		total_receivable = sum(flt(row.amount) for row in self.receivables)

		self.total_payable_amount = flt(total_payable, self.precision("total_payable_amount"))
		self.total_receivable_amount = flt(
			total_receivable + flt(self.total_asset_recovery_cost),
			self.precision("total_receivable_amount"),
		)

	def add_withheld_salary_slips(self):
		salary_slips = frappe.get_all(
			"Salary Slip",
			filters={
				"employee": self.employee,
				"status": "Withheld",
				"docstatus": ("!=", 2),
			},
			fields=["name", "net_pay"],
		)

		for slip in salary_slips:
			self.append(
				"payables",
				{
					"status": "Unsettled",
					"component": "Salary Slip",
					"reference_document_type": "Salary Slip",
					"reference_document": slip.name,
					"amount": slip.net_pay,
					"paid_via_salary_slip": 1,
				},
			)

	def create_component_row(self, components, component_type):
		for component in components:
			self.append(
				component_type,
				{
					"status": "Unsettled",
					"reference_document_type": component if component != "Bonus" else "Additional Salary",
					"component": component,
				},
			)

	def get_payable_component(self):
		return [
			"Gratuity",
			"Expense Claim",
			"Bonus",
			"Leave Encashment",
		]

	def get_receivable_component(self):
		receivables = ["Employee Advance"]
		if "lending" in frappe.get_installed_apps():
			receivables.append("Loan")
		return receivables

	def get_assets_movement(self):
		asset_movements = frappe.get_all(
			"Asset Movement Item",
			filters={"docstatus": 1},
			fields=["asset", "from_employee", "to_employee", "parent", "asset_name"],
			or_filters={"from_employee": self.employee, "to_employee": self.employee},
		)

		data = []
		inward_movements = []
		outward_movements = []
		for movement in asset_movements:
			if movement.to_employee and movement.to_employee == self.employee:
				inward_movements.append(movement)

			if movement.from_employee and movement.from_employee == self.employee:
				outward_movements.append(movement)

		for movement in inward_movements:
			outwards_count = [movement.asset for movement in outward_movements].count(movement.asset)
			inwards_counts = [movement.asset for movement in inward_movements].count(movement.asset)

			if inwards_counts > outwards_count:
				cost = frappe.db.get_value("Asset", movement.asset, "total_asset_cost")
				data.append(
					{
						"reference": movement.parent,
						"asset_name": movement.asset_name,
						"date": frappe.db.get_value("Asset Movement", movement.parent, "transaction_date"),
						"actual_cost": cost,
						"cost": cost,
						"action": "Return",
						"status": "Owned",
					}
				)
		return data

	@frappe.whitelist()
	def create_journal_entry(self):
		precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
		jv = frappe.new_doc("Journal Entry")
		jv.company = self.company
		jv.voucher_type = "Bank Entry"
		jv.posting_date = today()

		difference = self.total_payable_amount - self.total_receivable_amount

		for data in self.payables:
			if flt(data.amount) > 0 and not data.paid_via_salary_slip:
				account_dict = {
					"account": data.account,
					"debit_in_account_currency": flt(data.amount, precision),
					"user_remark": data.remark,
				}
				if data.reference_document_type in ["Expense Claim", "Gratuity"]:
					account_dict["party_type"] = "Employee"
					account_dict["party"] = self.employee

				jv.append("accounts", account_dict)

		for data in self.receivables:
			if flt(data.amount) > 0:
				account_dict = {
					"account": data.account,
					"credit_in_account_currency": flt(data.amount, precision),
					"user_remark": data.remark,
				}
				if data.reference_document_type == "Employee Advance":
					account_dict["party_type"] = "Employee"
					account_dict["party"] = self.employee

				jv.append("accounts", account_dict)

		for data in self.assets_allocated:
			if data.action == "Recover Cost":
				jv.append(
					"accounts",
					{
						"account": data.account,
						"credit_in_account_currency": flt(data.cost, precision),
						"party_type": "Employee",
						"party": self.employee,
						"user_remark": data.description,
					},
				)

		jv.append(
			"accounts",
			{
				"credit_in_account_currency": difference if difference > 0 else 0,
				"debit_in_account_currency": -(difference) if difference < 0 else 0,
				"reference_type": self.doctype,
				"reference_name": self.name,
			},
		)
		return jv

	def update_reference_document_payment_status(self, payable):
		doc = frappe.get_cached_doc(payable.reference_document_type, payable.reference_document)
		amount = payable.amount if self.docstatus == 1 and self.status == "Paid" else 0
		doc.db_set("paid_amount", amount)
		doc.set_status(update=True)

	def update_linked_payable_documents(self):
		"""update payment status in linked payable documents"""
		for payable in self.payables:
			if payable.reference_document_type in ["Gratuity", "Leave Encashment"]:
				self.update_reference_document_payment_status(payable)


@frappe.whitelist()
def get_account_and_amount(ref_doctype, ref_document, company):
	if not ref_doctype or not ref_document:
		return None

	if ref_doctype == "Salary Slip":
		salary_details = frappe.db.get_value(
			"Salary Slip", ref_document, ["payroll_entry", "net_pay"], as_dict=1
		)
		amount = salary_details.net_pay
		payable_account = (
			frappe.db.get_value("Payroll Entry", salary_details.payroll_entry, "payroll_payable_account")
			if salary_details.payroll_entry
			else None
		)
		return [payable_account, amount]

	if ref_doctype == "Gratuity":
		payable_account, amount = frappe.db.get_value("Gratuity", ref_document, ["payable_account", "amount"])
		return [payable_account, amount]

	if ref_doctype == "Expense Claim":
		details = frappe.db.get_value(
			"Expense Claim",
			ref_document,
			["payable_account", "grand_total", "total_amount_reimbursed", "total_advance_amount"],
			as_dict=True,
		)
		payable_account = details.payable_account
		amount = details.grand_total - (details.total_amount_reimbursed + details.total_advance_amount)
		return [payable_account, amount]

	if ref_doctype == "Loan":
		details = frappe.db.get_value(
			"Loan", ref_document, ["payment_account", "total_payment", "total_amount_paid"], as_dict=1
		)
		payment_account = details.payment_account
		amount = details.total_payment - details.total_amount_paid
		return [payment_account, amount]

	if ref_doctype == "Employee Advance":
		details = frappe.db.get_value(
			"Employee Advance",
			ref_document,
			["advance_account", "paid_amount", "claimed_amount", "return_amount"],
			as_dict=1,
		)
		payment_account = details.advance_account
		amount = details.paid_amount - (details.claimed_amount + details.return_amount)
		return [payment_account, amount]

	if ref_doctype == "Leave Encashment":
		amount = frappe.db.get_value("Leave Encashment", ref_document, "encashment_amount")
		payable_account = frappe.get_cached_value("Company", company, "default_payroll_payable_account")
		return [payable_account, amount]


def update_full_and_final_statement_status(doc, method=None):
	"""Updates FnF status on Journal Entry Submission/Cancellation"""
	status = "Paid" if doc.docstatus == 1 else "Unpaid"

	for entry in doc.accounts:
		if entry.reference_type == "Full and Final Statement":
			fnf = frappe.get_doc("Full and Final Statement", entry.reference_name)
			fnf.db_set("status", status)
			fnf.notify_update()
			fnf.update_linked_payable_documents()

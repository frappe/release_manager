frappe.listview_settings["User Story"] = {
	add_fields: ["status", "risk"],
	get_indicator(doc) {
		const colors = { Draft: "orange", Approved: "green", "Needs Change": "yellow", Rejected: "gray" };
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};

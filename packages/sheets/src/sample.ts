import type { SheetUseCase } from "./types.js";

export const SAMPLE_CSV = `Customer,Region,Account Status,Payment Status,Due Status,Invoice Amount,Revenue,Order Value,Order Date,Product,Sales Rep
Acme 4744,LATAM,active,paid,overdue,1200,1200,1200,2024-05-03,Cloud Backup,Ada
Beacon Ltd,EMEA,active,unpaid,current,800,800,800,2024-05-10,Security,Max
Cobalt Inc,LATAM,inactive,paid,current,500,500,500,2024-04-20,Analytics,Ada
Delta Co,NA,active,paid,overdue,2200,2200,2200,2024-05-15,Cloud Backup,Jules
Evergreen,EMEA,active,paid,current,1500,1500,1500,2024-06-02,Analytics,Max
Futura,LATAM,active,unpaid,overdue,3000,3000,3000,2024-05-23,Security,Ada
Horizon Labs,LATAM,active,paid,current,1800,1800,1800,2024-05-29,Analytics,Jules
Ion Works,NA,inactive,unpaid,overdue,700,700,700,2024-04-08,Cloud Backup,Max
Juniper Systems,EMEA,active,paid,overdue,2600,2600,2600,2024-05-06,Security,Ada
Kite Retail,NA,active,paid,current,950,950,950,2024-05-18,Analytics,Jules
Lumen Goods,LATAM,active,paid,overdue,4000,4000,4000,2024-06-11,Security,Ada
Mosaic Media,EMEA,inactive,paid,current,650,650,650,2024-05-12,Cloud Backup,Max
`;

const SUPPORT_CSV = `Ticket ID,Customer,Priority,Status,Team,Response Time Hours,Created Date,Satisfaction Score
T-1001,Acme 4744,high,open,Billing,6,2024-05-03,4
T-1002,Beacon Ltd,medium,closed,Platform,2,2024-05-04,5
T-1003,Cobalt Inc,high,open,Platform,9,2024-05-09,3
T-1004,Delta Co,low,closed,Billing,1,2024-04-28,5
T-1005,Evergreen,high,closed,Security,4,2024-05-14,4
T-1006,Futura,medium,open,Security,7,2024-05-18,3
T-1007,Horizon Labs,high,open,Billing,12,2024-06-02,2
T-1008,Ion Works,low,open,Platform,5,2024-05-22,4
`;

const INVENTORY_CSV = `Product,Category,Stock Status,Warehouse,Units On Hand,Unit Cost,Last Updated
Cloud Backup Starter,Backup,low stock,East,18,49,2024-05-01
Cloud Backup Pro,Backup,in stock,West,86,129,2024-05-03
Security Shield,Security,low stock,East,12,199,2024-05-05
Analytics Core,Analytics,in stock,Central,73,159,2024-05-08
Analytics Plus,Analytics,backordered,West,0,249,2024-05-09
Access Monitor,Security,in stock,Central,44,99,2024-05-11
Archive Vault,Backup,low stock,West,9,179,2024-05-14
`;

export const SHEET_USE_CASES: SheetUseCase[] = [
	{
		id: "revenue_ops",
		name: "Revenue Ops",
		description: "Invoices, regions, products, sales reps, and account status.",
		csv: SAMPLE_CSV,
		questions: [
			"total revenue by region",
			"top 5 customers by sales",
			"average order value in May",
			"how many paid invoices are overdue?",
			"show active accounts in LATAM",
		],
	},
	{
		id: "support_queue",
		name: "Support Queue",
		description:
			"Ticket status, priority, owning team, response time, and CSAT.",
		csv: SUPPORT_CSV,
		questions: [
			"how many open tickets are high priority?",
			"average response time hours by team",
			"show open tickets for Platform",
			"average satisfaction score in May",
		],
	},
	{
		id: "inventory",
		name: "Inventory",
		description:
			"Product stock status, warehouses, categories, and unit costs.",
		csv: INVENTORY_CSV,
		questions: [
			"total units on hand by warehouse",
			"show low stock products",
			"average unit cost by category",
			"how many products are backordered?",
		],
	},
];

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

const MARKETING_CSV = `Campaign,Channel,Status,Spend,Clicks,Leads,Start Date,Owner
Launch A,Search,active,"$1,200",300,42,2024-05-01,Ada
Launch B,Social,active,$800,220,31,2024-05-04,Max
Evergreen,Search,paused,$500,150,18,2024-04-20,Ada
Partner Push,Email,active,$300,90,14,2024-05-08,Jules
Retention Sprint,Email,paused,$450,105,22,2024-05-16,Jules
Conference Promo,Social,active,"$1,500",410,55,2024-06-02,Max
`;

const APPLICANT_CSV = `Applicant,Country,Status,Risk Tier,Has Passport,Wallet Balance,Created At
Ada Lovelace,Nigeria,approved,low,No,1200,2025-10-19
Grace Hopper,Ghana,review,medium,Yes,900,2025-10-18
Katherine Johnson,Nigeria,approved,low,Yes,3000,2025-10-17
Alan Turing,Germany,rejected,high,Yes,1800,2025-10-16
Dorothy Vaughan,Kenya,review,medium,No,1450,2025-10-15
Mary Jackson,,approved,low,Yes,2200,2025-10-14
`;

const ENGINEERING_BOM_CSV = `Item,Subsystem,Component,Specification,Quantity,Sourcing,Cost
1,EHD Subsystem,HV Power Module,12V DC input,20-30kV DC output,minimum 3A input current,1
2,EHD Subsystem,Needle Emitter,Sharp steel needle,1,Local market,100-500
6,EHD Subsystem,High-Voltage Silicone Wire,20kV rated silicone insulation,2m,Online,2000-5000
10,Active Cooling Core,Peltier Thermoelectric Module,TEC1-12706 module,1,Online,2500-4500
11,Active Cooling Core,Water Block,Copper block,1,Online,3000-6000
18,Water Cooling Loop,Silicone Tubing,10mm tubing,2m,Hardware store,1000-2500
19,Water Cooling Loop,Hose Clamps,10-12mm stainless steel,4,Hardware store,500-1000
22,Ozone Catalyst Subsystem,Catalyst Material,Manganese dioxide powder,2-3 batteries (yield ~10g powder),Recycled,0
23,Ozone Catalyst Subsystem,Catalyst Binder (optional),Small amount of water,10-20mL,N/A,0
30,Power System,Battery Cables (solar panel side),12 AWG stranded copper,4-6m,Hardware store,1000-2000
47,Assembly / Maintenance,Distilled Water,For reservoir filling,5L,Supermarket,500-1000
`;

const CLINIC_CSV = `Patient,Department,Status,Provider,Visit Cost,Wait Time Minutes,Visit Date,Follow Up Required
Nora Price,Cardiology,completed,Dr Reed,240,32,2024-05-01,Yes
Ibrahim Musa,Pediatrics,no-show,Dr Chen,0,0,2024-05-02,No
Maya Singh,Dermatology,completed,Dr Park,180,18,2024-05-04,No
Omar Bello,Cardiology,completed,Dr Reed,260,45,2024-05-08,Yes
Elena Rossi,Pediatrics,cancelled,Dr Chen,0,0,2024-05-09,No
Sam Green,Dermatology,completed,Dr Park,150,22,2024-06-01,Yes
Chika Okafor,Cardiology,no-show,Dr Reed,0,0,2024-06-03,No
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
	{
		id: "marketing_campaigns",
		name: "Marketing Campaigns",
		description:
			"Campaign spend, channels, leads, clicks, owners, and campaign status.",
		csv: MARKETING_CSV,
		questions: [
			"total spend by channel",
			"top 5 campaigns by clicks",
			"show active campaigns",
			"average leads by channel",
			"how many paused campaigns?",
		],
	},
	{
		id: "applicant_kyc",
		name: "Applicant KYC",
		description:
			"Applicant countries, review status, risk tiers, passports, balances, and created dates.",
		csv: APPLICANT_CSV,
		questions: [
			"how many approved applicants?",
			"show applicants not from Nigeria",
			"show applicants with wallet balance between 1000 and 2000",
			"how many unique countries are there?",
			"what is the maximum wallet balance?",
		],
	},
	{
		id: "engineering_bom",
		name: "Engineering BOM",
		description:
			"Subsystems, components, mixed quantity text, sourcing notes, and costs.",
		csv: ENGINEERING_BOM_CSV,
		questions: [
			"what is the item needed in most quantity?",
			"top 5 components by quantity",
			"list subsystems",
			"show components not in EHD Subsystem",
			"show components with quantity between 2 and 5",
		],
	},
	{
		id: "clinic_visits",
		name: "Clinic Visits",
		description:
			"Patient appointments, departments, visit status, providers, costs, waits, and follow-up flags.",
		csv: CLINIC_CSV,
		questions: [
			"total visit cost by department",
			"average wait time minutes by department",
			"show no-show patients",
			"how many completed appointments?",
			"average wait time minutes in May",
		],
	},
];

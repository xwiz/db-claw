import type { SheetUseCase } from "./types.js";

export const SAMPLE_CSV = `Customer,Region,Segment,Account Status,Payment Status,Due Status,Invoice Amount,Revenue,Order Value,Order Date,Product,Sales Rep,Contract Term,Renewal Risk
Acme 4744,LATAM,Mid-Market,active,paid,overdue,"$1,200",1200,1200,2024-05-03,Cloud Backup,Ada,Annual,medium
Beacon Ltd,EMEA,SMB,active,unpaid,current,$800,800,800,2024-05-10,Security,Max,Monthly,low
Cobalt Inc,LATAM,SMB,inactive,paid,current,$500,500,500,2024-04-20,Analytics,Ada,Monthly,high
Delta Co,NA,Enterprise,active,paid,overdue,"$2,200",2200,2200,2024-05-15,Cloud Backup,Jules,Annual,medium
Evergreen,EMEA,Enterprise,active,paid,current,"$1,500",1500,1500,2024-06-02,Analytics,Max,Annual,low
Futura,LATAM,Enterprise,active,unpaid,overdue,"$3,000",3000,3000,2024-05-23,Security,Ada,Annual,high
Horizon Labs,LATAM,Mid-Market,active,paid,current,"$1,800",1800,1800,2024-05-29,Analytics,Jules,Annual,low
Ion Works,NA,SMB,inactive,unpaid,overdue,$700,700,700,2024-04-08,Cloud Backup,Max,Monthly,high
Juniper Systems,EMEA,Mid-Market,active,paid,overdue,"$2,600",2600,2600,2024-05-06,Security,Ada,Annual,medium
Kite Retail,NA,SMB,active,paid,current,$950,950,950,2024-05-18,Analytics,Jules,Monthly,low
Lumen Goods,LATAM,Enterprise,active,paid,overdue,"$4,000",4000,4000,2024-06-11,Security,Ada,Annual,high
Mosaic Media,EMEA,SMB,inactive,paid,current,$650,650,650,2024-05-12,Cloud Backup,Max,Monthly,medium
Northstar Energy,NA,Enterprise,active,unpaid,overdue,"$5,200",5200,5200,2024-06-18,Security,Jules,Annual,high
Orchid Foods,LATAM,SMB,active,paid,current,"$1,100",1100,1100,2024-05-21,Analytics,Max,Monthly,low
Pioneer Bank,APAC,Enterprise,active,paid,overdue,"$3,400",3400,3400,2024-05-27,Cloud Backup,Priya,Annual,medium
Quartz Health,EMEA,Mid-Market,active,unpaid,current,"$2,750",2750,2750,2024-06-04,Security,Max,Annual,medium
River Logistics,LATAM,Mid-Market,active,paid,overdue,"$2,100",2100,2100,2024-05-30,Automation,Ada,Annual,medium
Summit Edu,APAC,SMB,active,paid,current,"$1,850",1850,1850,2024-06-06,Analytics,Priya,Monthly,low
`;

const SUPPORT_CSV = `Ticket ID,Customer,Priority,Status,Team,Product Area,Response Time Hours,First Reply SLA,Created Date,Satisfaction Score,Agent
T-1001,Acme 4744,high,open,Billing,Invoices,6,breached,2024-05-03,4,Nora
T-1002,Beacon Ltd,medium,closed,Platform,API,2,met,2024-05-04,5,Sam
T-1003,Cobalt Inc,high,open,Platform,Authentication,9,breached,2024-05-09,3,Sam
T-1004,Delta Co,low,closed,Billing,Invoices,1,met,2024-04-28,5,Nora
T-1005,Evergreen,high,closed,Security,Audit Logs,4,met,2024-05-14,4,Ivy
T-1006,Futura,medium,open,Security,Access Review,7,breached,2024-05-18,3,Ivy
T-1007,Horizon Labs,high,open,Billing,Collections,12,breached,2024-06-02,2,Nora
T-1008,Ion Works,low,open,Platform,API,5,met,2024-05-22,4,Sam
T-1009,Juniper Systems,urgent,open,Security,Incident Response,3,met,2024-05-24,4,Ivy
T-1010,Kite Retail,medium,closed,Billing,Refunds,2,met,2024-05-26,5,Nora
T-1011,Lumen Goods,high,open,Platform,Data Sync,11,breached,2024-06-04,2,Sam
T-1012,Mosaic Media,low,closed,Platform,Reports,3,met,2024-05-11,4,Sam
T-1013,Northstar Energy,high,open,Billing,Invoices,8,breached,2024-06-08,3,Nora
T-1014,Quartz Health,medium,pending customer,Security,SSO,6,met,2024-06-10,4,Ivy
T-1015,River Logistics,high,closed,Platform,Data Sync,5,met,2024-05-30,4,Sam
T-1016,Summit Edu,medium,open,Billing,Tax Forms,10,breached,2024-06-12,3,Nora
`;

const INVENTORY_CSV = `Product,SKU,Category,Stock Status,Warehouse,Units On Hand,Unit Cost,Reorder Point,Supplier,Last Updated
Cloud Backup Starter,BCK-001,Backup,low stock,East,18,49,30,Northwind,2024-05-01
Cloud Backup Pro,BCK-002,Backup,in stock,West,86,129,25,Northwind,2024-05-03
Security Shield,SEC-101,Security,low stock,East,12,199,20,IronGate,2024-05-05
Analytics Core,ANA-210,Analytics,in stock,Central,73,159,25,MetricWorks,2024-05-08
Analytics Plus,ANA-211,Analytics,backordered,West,0,249,15,MetricWorks,2024-05-09
Access Monitor,SEC-102,Security,in stock,Central,44,99,20,IronGate,2024-05-11
Archive Vault,BCK-003,Backup,low stock,West,9,179,18,Northwind,2024-05-14
Endpoint Sensor,SEC-103,Security,in stock,East,58,79,30,IronGate,2024-05-16
Pipeline Viewer,ANA-212,Analytics,low stock,Central,14,189,20,MetricWorks,2024-05-17
Restore Token Pack,BCK-004,Backup,in stock,East,160,19,80,Northwind,2024-05-19
Compliance Export,SEC-104,Security,backordered,West,0,299,10,IronGate,2024-05-22
Dashboard Lite,ANA-213,Analytics,in stock,West,41,89,25,MetricWorks,2024-05-24
Cold Storage Node,BCK-005,Backup,in stock,Central,32,399,12,Northwind,2024-06-01
Threat Rules Pack,SEC-105,Security,low stock,Central,7,149,15,IronGate,2024-06-03
Forecast Add-on,ANA-214,Analytics,in stock,East,25,129,18,MetricWorks,2024-06-05
`;

const MARKETING_CSV = `Campaign,Channel,Status,Spend,Clicks,Leads,Conversions,Start Date,Owner,Region
Launch A,Search,active,"$1,200",300,42,11,2024-05-01,Ada,NA
Launch B,Social,active,$800,220,31,7,2024-05-04,Max,EMEA
Evergreen,Search,paused,$500,150,18,4,2024-04-20,Ada,NA
Partner Push,Email,active,$300,90,14,3,2024-05-08,Jules,LATAM
Retention Sprint,Email,paused,$450,105,22,6,2024-05-16,Jules,NA
Conference Promo,Social,active,"$1,500",410,55,14,2024-06-02,Max,EMEA
Webinar Followup,Email,active,$700,180,36,9,2024-06-05,Jules,EMEA
Competitor Terms,Search,active,"$2,100",520,61,13,2024-06-09,Ada,NA
Partner Retargeting,Social,paused,$650,170,20,5,2024-05-20,Max,LATAM
Healthcare ABM,Display,active,"$1,850",260,48,12,2024-05-25,Priya,EMEA
Startup Nurture,Email,active,$520,130,27,6,2024-06-10,Jules,APAC
Midmarket Trial,Search,active,"$1,450",390,52,15,2024-06-14,Ada,LATAM
Brand Awareness,Display,paused,"$1,100",310,24,4,2024-04-30,Priya,NA
Renewal Push,Email,active,$400,95,19,8,2024-06-16,Jules,NA
Field Event,Social,active,"$2,300",480,70,18,2024-06-18,Max,APAC
`;

const APPLICANT_CSV = `Applicant,Country,Status,Risk Tier,Has Passport,Wallet Balance,Created At,Reviewer,Source
Ada Lovelace,Nigeria,approved,low,No,1200,2025-10-19,Rita,Referral
Grace Hopper,Ghana,review,medium,Yes,900,2025-10-18,Lee,Google Form
Katherine Johnson,Nigeria,approved,low,Yes,3000,2025-10-17,Rita,Referral
Alan Turing,Germany,rejected,high,Yes,1800,2025-10-16,Mina,Partner
Dorothy Vaughan,Kenya,review,medium,No,1450,2025-10-15,Lee,Google Form
Mary Jackson,,approved,low,Yes,2200,2025-10-14,Rita,CSV Import
Hedy Lamarr,Austria,review,medium,Yes,1750,2025-10-13,Mina,Partner
Annie Easley,United States,approved,low,No,980,2025-10-12,Rita,Referral
Joan Clarke,United Kingdom,rejected,high,Yes,2500,2025-10-11,Mina,Partner
Chien-Shiung Wu,China,approved,medium,Yes,4100,2025-10-10,Lee,CSV Import
Rosalind Franklin,United Kingdom,review,medium,No,1300,2025-10-09,Lee,Google Form
Mae Jemison,Nigeria,review,high,Yes,2100,2025-10-08,Mina,Referral
Gladys West,Ghana,approved,low,Yes,2600,2025-10-07,Rita,CSV Import
Radia Perlman,United States,approved,medium,Yes,3600,2025-10-06,Lee,Partner
`;

const ENGINEERING_BOM_CSV = `Item,Subsystem,Component,Specification,Quantity,Sourcing,Cost,Lead Time Days,Criticality
1,EHD Subsystem,HV Power Module,12V DC input to 20-30kV DC output,1,Online,"$18,000",21,high
2,EHD Subsystem,Needle Emitter,Sharp stainless steel needle,1,Local market,$500,2,high
3,EHD Subsystem,Ceramic Standoff,20kV rated insulator,8,Electronics supplier,"$2,400",10,medium
4,EHD Subsystem,High-Voltage Silicone Wire,20kV rated silicone insulation,2m,Online,"$3,500",7,high
5,Active Cooling Core,Peltier Thermoelectric Module,TEC1-12706 module,1,Online,"$3,200",14,medium
6,Active Cooling Core,Water Block,Copper block with G1/4 fitting,1,Online,"$4,800",14,medium
7,Active Cooling Core,Aluminum Cold Plate,120mm x 80mm machined plate,1,Machine shop,"$6,500",18,medium
8,Water Cooling Loop,DC Pump,12V brushless pump,1,Online,"$5,500",10,high
9,Water Cooling Loop,Silicone Tubing,10mm tubing,2m,Hardware store,"$1,400",3,medium
10,Water Cooling Loop,Hose Clamps,10-12mm stainless steel,6,Hardware store,$900,2,medium
11,Water Cooling Loop,Reservoir Bottle,500mL HDPE bottle,1,Local market,$700,1,low
12,Ozone Catalyst Subsystem,Catalyst Material,Manganese dioxide powder,2-3 batteries (yield about 10g),Recycled,$0,1,medium
13,Ozone Catalyst Subsystem,Catalyst Binder (optional),Small amount of water,10-20mL,N/A,$0,0,low
14,Power System,Battery Cables (solar panel side),12 AWG stranded copper,4-6m,Hardware store,"$1,500",3,high
15,Power System,Inline Fuse Holder,15A automotive blade fuse,2,Auto parts store,"$1,200",4,high
16,Controls,Temperature Sensor,DS18B20 waterproof probe,3,Electronics supplier,"$2,100",8,medium
17,Controls,Controller Board,ESP32 dev board,1,Electronics supplier,"$4,500",8,high
18,Assembly / Maintenance,M3 Stainless Screws,Assorted 8-16mm screws,24,Hardware store,"$1,000",2,low
19,Assembly / Maintenance,Distilled Water,For reservoir filling,5L,Supermarket,$800,1,low
`;

const CLINIC_CSV = `Patient,Department,Status,Provider,Visit Cost,Wait Time Minutes,Visit Date,Follow Up Required,Insurance Type
Nora Price,Cardiology,completed,Dr Reed,240,32,2024-05-01,Yes,Private
Ibrahim Musa,Pediatrics,no-show,Dr Chen,0,0,2024-05-02,No,Public
Maya Singh,Dermatology,completed,Dr Park,180,18,2024-05-04,No,Private
Omar Bello,Cardiology,completed,Dr Reed,260,45,2024-05-08,Yes,Private
Elena Rossi,Pediatrics,cancelled,Dr Chen,0,0,2024-05-09,No,Self Pay
Sam Green,Dermatology,completed,Dr Park,150,22,2024-06-01,Yes,Public
Chika Okafor,Cardiology,no-show,Dr Reed,0,0,2024-06-03,No,Public
Fatima Khan,Orthopedics,completed,Dr Stone,310,54,2024-05-12,Yes,Private
Luis Moreno,Cardiology,completed,Dr Reed,220,28,2024-05-15,No,Public
Anika Shah,Pediatrics,completed,Dr Chen,130,16,2024-05-16,No,Private
Theo Baker,Dermatology,no-show,Dr Park,0,0,2024-05-17,No,Self Pay
Zain Ali,Orthopedics,completed,Dr Stone,340,61,2024-05-23,Yes,Private
Rose Kim,Cardiology,cancelled,Dr Reed,0,0,2024-05-28,No,Private
Daniel Fox,Pediatrics,completed,Dr Chen,120,14,2024-06-06,Yes,Public
Lina Chen,Orthopedics,completed,Dr Stone,280,47,2024-06-11,No,Private
`;

export const SHEET_USE_CASES: SheetUseCase[] = [
	{
		id: "revenue_ops",
		name: "Revenue Ops",
		description: "Invoices, regions, products, sales reps, and account status.",
		csv: SAMPLE_CSV,
		questions: [
			"Compare total revenue by region for the pipeline review",
			"What are the top 5 customers by revenue?",
			"What was the average order value in May?",
			"How many overdue invoices have already been paid?",
			"Show me active LATAM accounts that need follow-up",
		],
	},
	{
		id: "support_queue",
		name: "Support Queue",
		description:
			"Ticket status, priority, owning team, response time, and CSAT.",
		csv: SUPPORT_CSV,
		questions: [
			"How many high priority tickets are still open?",
			"Compare average response time hours by team",
			"Show me open Platform tickets for triage",
			"What was the average satisfaction score for tickets created in May?",
		],
	},
	{
		id: "inventory",
		name: "Inventory",
		description:
			"Product stock status, warehouses, categories, and unit costs.",
		csv: INVENTORY_CSV,
		questions: [
			"Compare total units on hand by warehouse",
			"Show low stock products that need replenishment",
			"Compare average unit cost by category",
			"How many products are currently backordered?",
		],
	},
	{
		id: "marketing_campaigns",
		name: "Marketing Campaigns",
		description:
			"Campaign spend, channels, leads, clicks, owners, and campaign status.",
		csv: MARKETING_CSV,
		questions: [
			"Compare total spend by channel",
			"What are the top 5 campaigns by clicks?",
			"Show active campaigns for the weekly review",
			"Compare average leads by channel",
			"How many campaigns are paused right now?",
		],
	},
	{
		id: "applicant_kyc",
		name: "Applicant KYC",
		description:
			"Applicant countries, review status, risk tiers, passports, balances, and created dates.",
		csv: APPLICANT_CSV,
		questions: [
			"How many applicants have been approved?",
			"Show applicants whose country is not Nigeria",
			"Show applicants with wallet balance between 1000 and 2000",
			"How many unique countries are represented?",
			"What is the maximum wallet balance in the applicant pool?",
		],
	},
	{
		id: "engineering_bom",
		name: "Engineering BOM",
		description:
			"Subsystems, components, mixed quantity text, sourcing notes, and costs.",
		csv: ENGINEERING_BOM_CSV,
		questions: [
			"Which component is needed in the highest quantity?",
			"What are the top 5 components by quantity?",
			"List the subsystems represented in this BOM",
			"Show components not in EHD Subsystem",
			"Show components with quantity between 2 and 5 for batch planning",
		],
	},
	{
		id: "clinic_visits",
		name: "Clinic Visits",
		description:
			"Patient appointments, departments, visit status, providers, costs, waits, and follow-up flags.",
		csv: CLINIC_CSV,
		questions: [
			"Compare total visit cost by department",
			"Compare average wait time minutes by department",
			"Show no-show patients for follow-up",
			"How many appointments were completed?",
			"What was the average wait time minutes for May visits?",
		],
	},
];

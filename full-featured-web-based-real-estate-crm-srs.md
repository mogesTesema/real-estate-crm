   
**SOFTWARE REQUIREMENTS SPECIFICATION**

**Full-Featured Web-Based Real Estate CRM**

*For Brokerages, Agents, Developers, Property Managers & Investors*

 

Document Version: 2.1 (Admin mandatory requirements alignment)

Date: August 12, 2026

Status: Draft for Review

# **Table of Contents**

**Table of Contents**........................................................................................................................ 2

**1\. Introduction**............................................................................................................................ 4

**1.1 Purpose**............................................................................................................................. 4

**1.2 Scope**................................................................................................................................. 4

**1.3 Objectives**.......................................................................................................................... 4

**1.4 Key Challenges Addressed**................................................................................................... 5

**1.5 Definitions, Acronyms and Abbreviations**............................................................................ 5

**1.6 References**......................................................................................................................... 6

**1.7 Document Overview**........................................................................................................... 6

**2\. Overall Description**................................................................................................................... 7

**2.1 Product Perspective**............................................................................................................ 7

**2.2 Product Functions (Summary)**............................................................................................. 7

**2.3 User Classes and Characteristics**.......................................................................................... 7

**2.4 Operating Environment**...................................................................................................... 8

**2.5 Design and Implementation Constraints**.............................................................................. 8

**2.6 Assumptions and Dependencies**.......................................................................................... 8

**3\. System Features (Functional Requirements)**.............................................................................. 9

**3.1 Lead & Inquiry Management**........................................................................................... 9

**3.2 Contact & Customer Management**.................................................................................. 9

**3.3 Property & Listing Management**.................................................................................... 10

**3.4 Sales Pipeline & Opportunity Management**.................................................................... 10

**3.5 Rental & Lease Management**......................................................................................... 11

**3.6 Transaction, Invoicing & Commission Management**....................................................... 11

**3.7 Document Management & E-Signature**.......................................................................... 11

**3.8 Marketing Automation & Campaigns**............................................................................. 12

**3.9 Integrated Communications**.......................................................................................... 12

**3.10 MLS/IDX & Third-Party Portal Integration**.................................................................... 12

**3.11 Public Website & Client Portal**...................................................................................... 13

**3.12 Calendar, Tasks & Activity Management**...................................................................... 13

**3.13 Reporting, Dashboards & Analytics**.............................................................................. 13

**3.14 Property/Tenant Servicing & Maintenance**.................................................................. 14

**3.15 Teams, Branches & Company Organization**.............................................................. 14

**3.16 Mobile Access**............................................................................................................. 14

**3.17 Administration, Roles & Permissions**............................................................................ 14

**3.18 Notifications & Alerts**.................................................................................................. 15

**3.19 Integrations & Open API**.............................................................................................. 15

**3.20 AI-Assisted Features**.................................................................................................... 15

**4\. External Interface Requirements**............................................................................................. 16

**4.1 User Interfaces**................................................................................................................. 16

**4.2 Hardware Interfaces**.......................................................................................................... 16

**4.3 Software Interfaces**........................................................................................................... 16

**4.4 Communication Interfaces**................................................................................................ 16

**5\. Non-Functional Requirements**................................................................................................ 17

**5.1 Performance**.................................................................................................................... 17

**5.2 Scalability & Availability**.................................................................................................... 17

**5.3 Security**............................................................................................................................ 17

**5.4 Usability & Accessibility**..................................................................................................... 17

**5.5 Compliance & Data Privacy**............................................................................................... 17

**5.6 Maintainability & Extensibility**........................................................................................... 17

**5.7 Adoption, Training & Data Migration**................................................................................. 18

**6\. Proposed System Architecture**................................................................................................ 19

**6.1 High-Level Architecture**..................................................................................................... 19

**6.2 Suggested Technology Stack (Illustrative)**.......................................................................... 19

**7\. Core Data Model (Key Entities)**............................................................................................... 20

**8\. Primary Use Cases**.................................................................................................................. 21

**9\. Core Business Workflow**......................................................................................................... 22

**10\. Implementation Roadmap & MVP Phasing**............................................................................ 23

**Phase 1 — MVP: Foundation**............................................................................................... 23

**Phase 2 — Automation & Connectivity**................................................................................ 23

**Phase 3 — Financial Depth & Compliance**............................................................................ 23

**Phase 4 — Enterprise & Advanced Automation**.................................................................... 23

**11\. Appendix**............................................................................................................................. 24

**11.1 Assumptions Summary**................................................................................................... 24

**11.2 Revision History**.............................................................................................................. 24

 

# **1\. Introduction**

## **1.1 Purpose**

This Software Requirements Specification (SRS) defines the functional and non-functional requirements for a web-based, full-featured Real Estate Customer Relationship Management (CRM) system (hereafter "the System" or "RealEstate CRM"). The document is intended to guide the design, development, testing, and deployment of an **internal operational system for a single real estate company** (brokerage / agency) — covering sales, leasing, property management, marketing, and finance within that one organization. The System is **not** a multi-company SaaS product.

**Terminology note:** In this document, **tenant** always means a **rental tenant** (a contact who leases a property). It never means a software customer account or company partition. When stakeholders say **“multi-tenant data isolation”**, they mean **rental-tenant / portal-client isolation** (no rental tenant, landlord, buyer, or seller may see another client’s private records)—**not** SaaS multi-company tenancy.

The requirements captured here were compiled from (a) analysis of Odoo's Real Estate / Property Management and CRM modules, (b) a comparative review of leading real estate CRM platforms including Follow Up Boss, kvCORE/Boldtrail, Wise Agent, Lofty, Sierra Interactive, Real Geeks, Top Producer, Lone Wolf (Propertybase), BoomTown, REsimpli, and Agora, and (c) consolidation with two independently drafted SRS versions produced by other AI assistants, so that the resulting product is competitive with, and in several areas more complete than, existing solutions.

## **1.2 Scope**

The System is a cloud-hosted, browser-accessible CRM built specifically for **one real estate company**. It must handle the full lifecycle of a real estate transaction and relationship — from first inquiry/lead capture, through property matching, viewings, negotiation, contracting, closing, and after-sale/lease servicing — covering the company's business lines as configured:

•    Real estate sales (residential & commercial) and agents

•    Property development (off-plan / new project sales and construction-linked payment plans), when the company operates that line

•    Property management / leasing (landlords, rental tenants, recurring rent, maintenance)

•    Investment / wholesale deal sourcing, when applicable

•    Multi-branch and multi-team operation **within the same company**

The System will be delivered as a responsive web application (desktop and mobile browser) with a companion mobile app experience, a public-facing property portal/website module, an administration console, and an open API for third-party integration (MLS/IDX, portals such as Zillow/Realtor.com/PropertyFinder/Bayut, accounting systems, e-signature providers, and communication channels).

Out of scope for version 1.0: multi-company SaaS tenancy; full double-entry general-ledger accounting (the System will integrate with external accounting/ERP systems rather than replace them); core construction/project-management (BIM, scheduling) beyond basic milestone tracking; and country-specific legal document authoring (the System stores and manages documents/e-signatures but does not provide legal advice).

## **1.3 Objectives**

•    Centralize all real estate operations — leads, properties, deals, contracts, and finance — in a single system of record.

•    Automate the lead-to-sale (and lead-to-lease) pipeline so no inquiry is lost and every follow-up happens on time.

•    Improve agent productivity by removing manual data entry and giving a single screen for calls, messages, tasks, and documents.

•    Track the full property lifecycle from listing/acquisition through to closing, handover, or ongoing lease management.

•    Enable analytics-driven decision-making for managers and owners through real-time dashboards and forecasting.

•    Achieve high user adoption through an intuitive interface, since industry experience shows that a large share of CRM rollouts stall due to poor usability and change management rather than missing features.

## **1.4 Key Challenges Addressed**

The System is designed to directly solve the operational problems most commonly reported by real estate businesses running on spreadsheets, generic CRMs, or disconnected point tools:

•    Data fragmentation: agents currently switch between spreadsheets, personal inboxes, WhatsApp, and portal dashboards, causing lost leads and missed follow-ups.

•    Data loss from manual entry: a very large share of agent conversations (calls, WhatsApp, walk-ins) never make it into a traditional CRM because logging them is too much friction — the System must make logging automatic wherever possible (Section 3.9).

•    Poor lead response time: leads frequently go cold because they are not captured, routed, or followed up on instantly — addressed by automated capture and routing (Section 3.1).

•    Lack of pipeline visibility: managers often cannot see real-time deal progress or agent performance — addressed by the pipeline and reporting modules (Sections 3.4, 3.13).

•    Integration gaps between sales, contracts, and finance: disconnected tools cause reconciliation errors and administrative overhead — addressed by the unified data model (Section 7\) and transaction/commission module (Section 3.6).

## **1.5 Definitions, Acronyms and Abbreviations**

| Term | Definition |
| :---- | :---- |
| CRM | Customer Relationship Management |
| SRS | Software Requirements Specification |
| MLS | Multiple Listing Service — a database used by real estate brokers to share property listings |
| IDX | Internet Data Exchange — feed that lets MLS listings be displayed on a broker's/agent's own website |
| Lead | A prospective buyer, seller, tenant, landlord, or investor who has expressed interest |
| Opportunity / Deal | A qualified lead progressing through a sales or leasing pipeline toward a closed transaction |
| Listing | A property record marketed for sale or rent |
| RBAC | Role-Based Access Control |
| SLA | Service Level Agreement |
| API | Application Programming Interface |
| SSO | Single Sign-On |
| PII | Personally Identifiable Information |
| GCI | Gross Commission Income |
| Tenant (rental) | A contact who leases a property (never a software customer account) |
| Multi-tenant isolation (product sense) | Isolation between rental tenants / portal clients so one client cannot access another client’s data. **Not** SaaS multi-company partitioning |
| Super Admin | Highest authority in the company System: overrides all other roles for configuration, security, and user governance |
| Company / Organization | The single real estate business that operates this System |

## **1.6 References**

•    Odoo S.A. — CRM, Sales, Rental and Property Management application documentation (odoo.com/industries/property-management)

•    IEEE Std 830-1998 — Recommended Practice for Software Requirements Specifications (structural reference for this document)

•    Comparative feature research on real estate CRM platforms: Follow Up Boss, kvCORE/Boldtrail, Wise Agent, Lofty, Sierra Interactive, Real Geeks, Top Producer, Lone Wolf/Propertybase, BoomTown, REsimpli, Agora, Pipedrive, HubSpot, monday.com (2026 market comparisons)

•    General Data Protection Regulation (GDPR) and applicable local real estate / data-privacy regulations

•    Two supplementary AI-generated SRS drafts (produced by other assistants) covering CRM-plus-ERP hybrid architecture, phased MVP rollout, and real-estate-specific pain points, consolidated into this version

## **1.7 Document Overview**

Section 2 describes the product at a high level: perspective, user classes, operating environment, and constraints. Section 3 is the core of this SRS and enumerates functional requirements module-by-module. Section 4 covers external interface requirements. Section 5 defines non-functional requirements (performance, security, scalability, usability, compliance). Section 6 proposes a reference system architecture and technology stack. Section 7 outlines the core data model. Section 8 lists primary use cases. Section 9 contains appendices.

# **2\. Overall Description**

## **2.1 Product Perspective**

The System is a new, standalone, **single-company** internal Real Estate CRM — deployed and operated for one brokerage/agency (with optional multi-branch, multi-team structure inside that company). It is inspired by the modular philosophy of Odoo (a single suite covering CRM, Sales, Rental, Invoicing, Website and Project modules under one data model) but is purpose-built end-to-end for real estate rather than adapted from a generic ERP. It must interoperate with, rather than duplicate, specialist third-party systems such as MLS/IDX providers, e-signature platforms (DocuSign, Dotloop), accounting/ERP systems, and SMS/email/telephony gateways.

## **2.2 Product Functions (Summary)**

At a high level the System shall provide:

•    Omni-channel lead capture, scoring, routing and nurturing

•    Centralized contact and account management for buyers, sellers, tenants, landlords, and investors

•    Property/listing management with rich media, availability, and status tracking

•    Visual, configurable sales and leasing pipelines (Kanban) with deal/opportunity tracking

•    Lease and rental contract management with recurring invoicing and renewals

•    Transaction management: offers, contracts, commissions, and closing checklists

•    Document management with templates and e-signature

•    Marketing automation: drip campaigns, email/SMS, landing pages, and a public property portal

•    Integrated communications: call, SMS, WhatsApp, and email logged against each contact

•    MLS/IDX and third-party portal integrations for listing syndication and lead import

•    Calendar, task, and activity management with automated reminders

•    Reporting, dashboards, and forecasting for agents, teams, and management

•    Property/tenant servicing: maintenance requests and a tenant/owner self-service portal

•    Multi-branch, multi-team, and multi-currency support for the company's branches and international operations

•    Mobile-friendly access and a dedicated mobile app

•    Administration: roles, permissions, customization, and audit trail

•    Open REST API and webhook framework for integrations

•    AI-assisted features: lead scoring, next-best-action suggestions, and chatbot intake

## **2.3 User Classes and Characteristics**

| User Class | Characteristics / Responsibilities |
| :---- | :---- |
| Super Admin | **Highest authority** in the System. Full company control: users, roles, security, integrations, backups, global settings, and audit oversight. No other role outranks Super Admin. |
| Branch / Team Manager | Registers and oversees Broker/Agency Owners under their branch/team; lead routing, pipeline oversight, branch performance. |
| Broker / Agency Owner | Registered under a Branch/Team Manager. Registers and manages Sales/Leasing Agents under their agency; commissions, inventory, and agent performance. |
| Sales / Leasing Agent | Registered under a Broker/Agency Owner. Day-to-day user: own leads, contacts, listings, deals, calendar, communications, and **mandatory field GPS tracking**. |
| Property Manager | Manages assigned properties only: leases, rent schedules, maintenance, tenant/owner communication. **Every property must have one assigned Property Manager.** |
| Marketing Staff | Builds campaigns, landing pages, email/SMS drips, and manages the public website/portal content. |
| Finance / Accounts Staff | Views/exports commission and invoicing data; reconciles with external accounting systems. |
| Portal User (Buyer / Seller / Rental Tenant / Landlord) | External client with limited portal access **only after a completed contract** (sale, lease, or other closed lead type)—not any open lead. Sees only their own records. |
| API / Integration Client | External system consuming the System's API (MLS feed, accounting package, website). |

## **2.4 Operating Environment**

•    The System shall run as a cloud-hosted web application accessible via modern browsers (Chrome, Edge, Safari, Firefox — current and previous major version) on desktop, tablet, and mobile.

•    The System shall be responsive and usable on screen widths from 360px (mobile) upward, and shall offer native-like mobile apps (iOS and Android) built on the same API.

•    Server-side components shall be deployable on standard Linux-based cloud infrastructure (containerized) and shall support horizontal scaling.

•    The System must support multiple languages, currencies, date/number formats, and time zones for international deployments.

## **2.5 Design and Implementation Constraints**

•    Access control must enforce **role- and ownership-based** data visibility within the single company (e.g., agents see their own records; branch managers see their branch). **Rental-tenant / portal-client isolation is mandatory:** no portal client may access another client’s private data. There is no multi-company / SaaS tenancy partition.

•    **Super Admin is the highest authority** and may manage all users and security policy within the company. Organizational registration hierarchy is: Super Admin → Branch/Team Manager → Broker/Agency Owner → Sales/Leasing Agent (with Property Manager / Marketing / Finance as staff roles under company governance).

•    All external integrations (MLS/IDX, e-signature, SMS/email, payment) must be implemented via abstracted connector modules so providers can be swapped without core redesign.

•    The data model must remain extensible via custom fields/objects without schema migrations for every company-specific requirement (similar to Odoo's customizable field approach).

•    The System must comply with data protection regulations relevant to the deployment region (e.g., GDPR) including data residency where required.

## **2.6 Assumptions and Dependencies**

•    Users have reliable internet access; core workflows should degrade gracefully (offline queuing) on the mobile app only, not on the web app.

•    MLS/IDX data, where used, is licensed separately by the customer; the System only provides the integration mechanism.

•    Third-party services (SMS gateways, email providers, e-signature, payment gateways) are assumed available via API and billed separately unless bundled.

•    Legal validity of e-signatures and contracts is governed by local law; the System provides the technical capability, not legal certification.

# **3\. System Features (Functional Requirements)**

Each subsystem below lists its purpose and numbered functional requirements (FR). Requirement IDs follow the pattern FR-\<section\>.\<sequence\> for traceability into design and test documents.

### **3.1 Lead & Inquiry Management**

Captures and manages every prospective buyer, seller, tenant, or landlord inquiry from any channel, and converts qualified leads into opportunities — comparable to Odoo's Inquiry-to-Opportunity flow and industry-standard lead capture found in Follow Up Boss, Real Geeks, and kvCORE.

**3.1.1**  The System shall capture leads from web forms, landing pages, phone calls, email, WhatsApp, live chat/chatbot, walk-ins, and manual entry.

**3.1.2**  The System shall automatically import leads from connected portals (MLS/IDX, Zillow, Realtor.com, PropertyFinder, Bayut, Facebook/Instagram lead ads) via configured integrations.

**3.1.3**  The System shall de-duplicate incoming leads against existing contacts using configurable matching rules (phone, email, name).

**3.1.4**  The System shall support configurable lead types: Buy, Sell, Rent-Out, Rent-In, Investment.

**3.1.5**  The System shall provide automatic and manual lead routing/assignment (round-robin, territory-based, workload-based, or rule-based) to agents or teams.

**3.1.6**  The System shall provide lead scoring (rule-based and AI-assisted) based on source, engagement, budget, and behavior.

**3.1.7**  The System shall log full inquiry details: property type, budget range, preferred location(s), timeline, financing status, and notes.

**3.1.8**  The System shall allow converting a qualified lead/inquiry into an Opportunity with one action, carrying over all captured data.

**3.1.9**  The System shall flag and alert on leads with no follow-up activity within a configurable SLA window.

**3.1.10**  The System shall automatically detect likely duplicate leads/contacts (matching phone, email, or name+location) at the point of capture and flag them for merge, preventing two agents from independently contacting the same prospect.

**3.1.11**  The System shall treat two leads as duplicates when they match on phone and/or email and/or name **and** target the **same property** with the **same lead type** (Buy, Sell, Rent-In, Rent-Out, Investment), and shall flag them for merge or reassignment.

**3.1.12**  The System shall send an instant, automated acknowledgment (email/SMS/WhatsApp) to a prospect immediately upon lead capture, to set the right first impression while human follow-up is pending.

### **3.2 Contact & Customer Management**

Maintains a single, centralized record for every person and organization the business deals with, distinguishing buyers, sellers, tenants, landlords, investors, and vendors — extending Odoo's approach of managing buyers/sellers as distinct customer types.

**3.2.1**  The System shall maintain a 360-degree contact profile: personal/company details, relationship type(s), communication history, documents, linked properties, and linked deals.

**3.2.2**  The System shall support one contact holding multiple relationship roles simultaneously (e.g., a person who is both a past seller and a current buyer).

**3.2.3**  The System shall allow tagging, custom fields, and segmentation (by location preference, budget, source, VIP status, etc.).

**3.2.4**  The System shall track full interaction history: calls, emails, SMS, meetings, site visits, and notes, in a unified timeline per contact.

**3.2.5**  The System shall support household/family and company/organization grouping of related contacts.

**3.2.6**  The System shall support importing and exporting contacts via CSV/Excel with field mapping and de-duplication.

**3.2.7**  The System shall provide a merge function for duplicate contact records with full audit history retained.

**3.2.8**  The System shall allow linking contacts to referral sources and track referral-based commissions.

### **3.3 Property & Listing Management**

Manages the full property catalog — for sale, for rent, or under development — with rich details, media, and status, equivalent to and extending Odoo's Properties app.

**3.3.1**  The System shall support creation and management of property listings for residential, commercial, industrial, and land categories.

**3.3.2**  The System shall capture structured property attributes: address/geolocation, price, area (built-up/carpet), bedrooms/bathrooms, amenities, year built, floor/unit, parking, and custom attributes per property type.

**3.3.3**  The System shall support multiple photos, floor plans, virtual tours (360°/video embed), and brochure/PDF attachments per listing.

**3.3.4**  The System shall track listing status (Draft, Active, Under Offer, Reserved, Sold, Rented, Off-Market, Expired) with full status-change history.

**3.3.5**  The System shall support both exclusive and open/shared listings, and multi-agent/co-listing assignment.

**3.3.6**  The System shall support project/development hierarchies (Project → Building/Phase → Unit) for off-plan and multi-unit developments, with unit-level availability and payment-plan tracking.

**3.3.7**  The System shall provide advanced search and filtering of the property database by any attribute, price range, or location radius/map-drawn area.

**3.3.8**  The System shall automatically match new and existing leads to matching properties based on stated buyer/tenant criteria, and notify the responsible agent.

**3.3.9**  The System shall support owner records per listing (linking to a Contact) with commission/mandate terms.

**3.3.10**  The System shall require every property to have an assigned Property Manager (`managed_by`); property create/update without a Property Manager shall be rejected.

**3.3.11**  The System shall track property viewing/showing appointments and capture post-viewing feedback.

### **3.4 Sales Pipeline & Opportunity Management**

Provides the visual deal pipeline through which every sale opportunity is progressed, mirroring Odoo CRM's Kanban pipeline and the pipeline features of Pipedrive, Follow Up Boss, and Lofty.

**3.4.1**  The System shall provide a configurable, drag-and-drop Kanban pipeline with custom stages per business line (e.g., Sales, Leasing, Off-Plan).

**3.4.2**  The System shall allow multiple pipelines to run in parallel (e.g., separate pipelines for residential sales, commercial leasing, new-project sales).

**3.4.3**  The System shall track opportunity value, expected close date, probability, and weighted revenue forecast per stage.

**3.4.4**  The System shall log a mandatory reason and next action when a deal moves stage or is marked Lost.

**3.4.5**  The System shall trigger stage-based automated tasks/reminders (e.g., 'send offer letter' on entering Negotiation stage).

**3.4.6**  The System shall provide a forecast/revenue dashboard aggregating pipeline value by agent, team, branch, and period.

**3.4.7**  The System shall support linking one or more properties to an opportunity, and one opportunity to a specific matched property once identified.

### **3.5 Rental & Lease Management**

Manages the leasing lifecycle for property management operations, from tenant application to recurring invoicing and renewal — matching Odoo's Rental contract features.

**3.5.1**  The System shall support creation of lease/rental contracts by selecting a tenant, a property/unit, and lease terms (start/end date, rent amount, deposit, escalation).

**3.5.2**  The System shall support tenant screening workflows (application form, background/credit check integration, approval/rejection tracking).

**3.5.3**  The System shall automatically generate recurring rent invoices per the contract's billing schedule and send payment reminders.

**3.5.4**  The System shall track security deposits, move-in/move-out meter readings and condition reports.

**3.5.5**  The System shall alert on upcoming lease expirations and support one-click renewal or termination workflows.

**3.5.6**  The System shall track landlord/owner statements including rent collected, management fees, and net remittance.

**3.5.7**  The System shall support multiple lease types: long-term residential, short-term/holiday rental, and commercial leasing with percentage-rent or escalation clauses.

### **3.6 Transaction, Invoicing & Commission Management**

Handles the financial lifecycle of a deal: offers, contracts, invoicing hooks, and commission calculation/splits — extending Odoo's brokerage payment tracking.

**3.6.1**  The System shall support creation of offers/proposals with configurable templates and approval workflow.

**3.6.2**  The System shall generate sale/lease agreements and invoices directly from a won opportunity or lease contract.

**3.6.3**  The System shall track payment milestones/installment plans for off-plan property sales.

**3.6.4**  The System shall calculate agent/broker commission automatically based on configurable rules (flat %, tiered, split between listing/selling agent, referral cut, franchise fee).

**3.6.5**  The System shall track commission payment status (Pending, Approved, Paid) per agent and per deal.

**3.6.6**  The System shall provide closing checklists (configurable per transaction type) with task ownership and due dates.

**3.6.7**  The System shall automatically apply the correct tax/VAT rate on commission invoices based on the tenant's configured jurisdiction (e.g., a configurable percentage such as 5% UAE VAT on brokerage commissions) and generate tax-summary reports.

**3.6.8**  The System shall track security deposits, installment schedules, and flag/penalize (per configurable rules) late payments on both sale installment plans and rent collection.

**3.6.9**  The System shall support integration/export to external accounting or ERP systems (e.g., via API or standard file export) rather than duplicating full ledger accounting.

### **3.7 Document Management & E-Signature**

Centralizes all contracts, disclosures, and property documents, with templating and legally-binding e-signature, as offered by Odoo Document Management and dedicated tools like DocuSign/Dotloop.

**3.7.1**  The System shall provide a document repository per contact, property, and deal with folder structure and version history.

**3.7.2**  The System shall provide a document template engine with merge fields (contact, property, deal data) for contracts, offers, and disclosures.

**3.7.3**  The System shall integrate with e-signature providers (e.g., DocuSign, Dotloop, or an equivalent native e-sign capability) with signer tracking and completion status.

**3.7.4**  The System shall support role-based access control on documents (e.g., confidential financial documents visible only to Finance and Management).

**3.7.5**  The System shall retain an immutable audit trail of document views, edits, and signature events.

### **3.8 Marketing Automation & Campaigns**

Enables lead nurturing and demand generation across email, SMS, and web — matching capabilities in Wise Agent, kvCORE, HubSpot, and Odoo Marketing.

**3.8.1**  The System shall support automated drip campaigns (email/SMS) triggered by lead source, stage, tag, or inactivity.

**3.8.2**  The System shall provide a landing-page and property-microsite builder (no-code) for individual listings or campaigns.

**3.8.3**  The System shall support bulk email/SMS sending with templates, personalization tokens, and delivery/open/click tracking.

**3.8.4**  The System shall support saved-search alerts that automatically email/SMS a lead when a new matching property is listed.

**3.8.5**  The System shall integrate with social media ad platforms (Facebook/Instagram, Google) for lead capture and basic ROI tracking.

**3.8.6**  The System shall support a client-facing market/CMA (comparative market analysis) report generator.

### **3.9 Integrated Communications**

Provides multi-channel communication logged automatically against the contact/deal timeline, as offered by Follow Up Boss, Lofty, and Wise Agent.

**3.9.1**  The System shall provide click-to-call dialing with automatic call logging, recording (where legally permitted), and disposition notes.

**3.9.2**  The System shall support two-way SMS and WhatsApp messaging from within the contact record.

**3.9.3**  The System shall support two-way email sync (IMAP/SMTP or provider API) so emails sent/received are logged against the contact automatically.

**3.9.4**  The System shall provide message templates and canned responses for common scenarios (new inquiry, viewing confirmation, offer received).

**3.9.5**  The System shall support an embeddable website live-chat/chatbot that creates or updates a lead automatically.

**3.9.6**  The System shall provide an internal team chat/comment thread on each lead, deal, or property so agents, managers, and admins can collaborate without leaving the record.

### **3.10 MLS/IDX & Third-Party Portal Integration**

Connects the System to external listing data sources and syndicates listings outward — a capability considered essential across nearly all reviewed platforms.

**3.10.1**  The System shall support inbound IDX/MLS feed integration to import and refresh listing data on a configurable schedule.

**3.10.2**  The System shall support outbound listing syndication (push) to third-party portals (e.g., Zillow, Realtor.com, PropertyFinder, Bayut, or regional equivalents) via API or feed export.

**3.10.3**  The System shall reconcile listing status changes (sold/rented/expired) bidirectionally where the integration partner supports it.

**3.10.4**  The System shall log and alert on feed/integration errors (e.g., rejected listings, mapping failures).

### **3.11 Public Website & Client Portal**

Provides a public-facing property search experience and a secure self-service portal for buyers, sellers, rental tenants, and landlords **who have completed contracts**.

**3.11.1**  The System shall provide a public, SEO-friendly property search website (or embeddable widget for an existing site) with map, filter, and saved-search capability.

**3.11.2**  The System shall grant Portal User login **only** to clients with a **completed contract** (closed sale, active/signed lease, or other closed lead/transaction type)—**not** to open/unconverted leads.

**3.11.3**  The System shall provide a secure buyer portal (post-contract/closing where applicable) to view shared documents, closing status, and related deal records owned by that contact.

**3.11.4**  The System shall provide a secure seller/landlord portal to view listing performance (views, inquiries, showings) and, for landlords, rent/statement history for properties they own.

**3.11.5**  The System shall provide a rental-tenant portal to view lease terms, rent history, and submit maintenance requests, feeding directly into the maintenance workflow (Section 3.14).

**3.11.6**  The System shall enforce **rental-tenant / portal-client isolation**: each portal user may access only their own linked contact’s records; no portal user may view another client’s private data.

### **3.12 Calendar, Tasks & Activity Management**

Keeps every agent's day organized and ensures no follow-up is missed, addressing the industry's most commonly cited pain point.

**3.12.1**  The System shall provide a shared and personal calendar for viewings, meetings, calls, and internal tasks, with sync to Google/Outlook calendars.

**3.12.2**  The System shall auto-generate tasks/reminders from pipeline stage changes, lease expirations, and SLA breaches.

**3.12.3**  The System shall provide a daily/weekly 'next best action' or 'follow-up coach' view prioritizing which contacts to reach out to.

**3.12.4**  The System shall support recurring tasks and team task assignment with due-date and completion tracking.

### **3.13 Reporting, Dashboards & Analytics**

Gives individuals and management real-time visibility into performance, matching the analytics depth of BoomTown, Lofty, and Odoo's reporting engine.

**3.13.1**  The System shall provide role-based dashboards (Agent, Manager, Owner) showing KPIs: leads, conversion rate, pipeline value, closed deals, GCI, and response time.

**3.13.2**  The System shall provide standard reports: lead source ROI, agent leaderboard, listing inventory aging, occupancy rate, and commission summaries.

**3.13.3**  The System shall support custom report/dashboard building with drag-and-drop fields, filters, and scheduled email delivery.

**3.13.4**  The System shall support export of any report to Excel/CSV/PDF.

### **3.14 Property/Tenant Servicing & Maintenance**

Supports ongoing property management operations after lease signing, matching Odoo's maintenance/work-order capability.

**3.14.1**  The System shall allow tenants (via portal) or staff to log maintenance requests with description, priority, and photos.

**3.14.2**  The System shall track maintenance work orders through status (New, Assigned, In Progress, Completed) with vendor/contractor assignment.

**3.14.3**  The System shall maintain a vendor/contractor directory with linked work-order history and costs.

**3.14.4**  The System shall link maintenance costs back to the relevant property/owner statement.

### **3.15 Teams, Branches & Company Organization**

Supports a real estate company that operates across multiple branches or teams from one instance, with a clear registration and authority hierarchy.

**3.15.1**  The System shall treat **Super Admin as the highest authority** in the company (user governance, security policy, integrations, and system configuration). No other role may override Super Admin privileges.

**3.15.2**  The System shall support hierarchical registration: Super Admin → Branch/Team Manager → Broker/Agency Owner → Sales/Leasing Agent. Branch/Team Managers register Broker/Agency Owners; Broker/Agency Owners register Sales/Leasing Agents.

**3.15.3**  The System shall support organizational structure Company → Branch → Team, with users assigned to branch/team as applicable.

**3.15.4**  The System shall support data visibility rules by hierarchy and role scope (e.g., a branch manager sees their branch's data; agents see own records unless shared).

**3.15.5**  The System shall support multi-currency and multi-language operation, with per-branch localization of currency, tax, and date formats.

**3.15.6**  The System shall support configurable commission splits between agent, team, branch, and head office.

### **3.16 Mobile Access**

Ensures agents, who are frequently in the field, have full functionality on mobile devices, including mandatory sales-officer GPS tracking.

**3.16.1**  The System shall provide a fully responsive mobile web experience for all core workflows.

**3.16.2**  The System shall provide native iOS and Android applications with push notifications for new leads, messages, and task reminders.

**3.16.3**  The System shall support offline capture of viewing feedback and notes on the mobile app, syncing automatically when connectivity resumes.

**3.16.4**  The System shall support click-to-call, camera-based property photo upload, and geolocation-based check-in at property viewings from the mobile app.

**3.16.5**  The System shall require Sales/Leasing Agents (sales officers) to enable **GPS location tracking** when they leave for a sale, viewing, or other field sales activity. Field activity cannot be started or marked in-progress without an active tracking session (subject to device OS permission).

**3.16.6**  The System shall record a GPS tracking session per field activity (start/end time, agent, related lead/deal/property/viewing when known) and periodic location points (timestamp, latitude, longitude, accuracy) for compliance and manager oversight.

**3.16.7**  The System shall allow authorized managers/owners/admins to view an agent’s active/recent field GPS track within policy retention limits, and shall log access to location history in the audit trail.

### **3.17 Administration, Roles & Permissions**

Provides the configuration backbone for security and customization.

**3.17.1**  The System shall provide role-based access control (RBAC) with predefined roles (Section 2.3) and support for custom roles/permission sets. Super Admin remains the highest authority and cannot be locked out by custom role configuration.

**3.17.2**  The System shall provide field-level and record-level (row) security (e.g., an agent sees only their own leads unless granted broader access).

**3.17.3**  The System shall provide an audit log of all create/update/delete actions on key records (contacts, deals, listings, documents) with user and timestamp.

**3.17.4**  The System shall support custom fields, custom object/entity creation, and configurable picklists without requiring code changes.

**3.17.5**  The System shall support Single Sign-On (SSO) via SAML/OAuth2 for company deployments that require it.

**3.17.6**  The System shall enforce rental-tenant / portal-client isolation as a hard security rule (no cross-client private record access), independent of SaaS multi-company tenancy (which is out of scope).

### **3.18 Notifications & Alerts**

Keeps users and clients informed proactively across channels.

**3.18.1**  The System shall send in-app, email, and push notifications for new leads, assigned tasks, upcoming appointments, and SLA breaches.

**3.18.2**  The System shall allow each user to configure their own notification channel preferences and quiet hours.

**3.18.3**  The System shall send automated client-facing notifications (e.g., viewing confirmations, rent due reminders, document signed confirmations).

### **3.19 Integrations & Open API**

Ensures the System fits into an existing technology stack rather than forcing replacement of all other tools.

**3.19.1**  The System shall expose a documented REST API (with authentication via API keys/OAuth2) covering all core objects (contacts, properties, deals, leases, documents).

**3.19.2**  The System shall provide outbound webhooks for key events (new lead, stage change, deal won/lost, document signed).

**3.19.3**  The System shall provide native or Zapier/Make-style connector integration for common third-party tools (Google Workspace, Microsoft 365, Slack, accounting packages, payment gateways).

**3.19.4**  The System shall provide a sandbox/test environment for integration development separate from production data.

### **3.20 AI-Assisted Features**

Incorporates modern AI capabilities that competing platforms (Lofty, kvCORE, Top Producer) increasingly position as differentiators.

**3.20.1**  The System shall provide AI-based lead scoring that ranks leads by likelihood to transact.

**3.20.2**  The System shall provide an AI 'next-best-action' assistant suggesting who to contact and what to say.

**3.20.3**  The System shall provide an AI drafting assistant for emails/SMS/listing descriptions.

**3.20.4**  The System shall provide a website/WhatsApp chatbot capable of qualifying a lead (budget, location, timeline) before handing off to an agent.

# **4\. External Interface Requirements**

## **4.1 User Interfaces**

•    The web application shall use a clean, modern, responsive layout consistent across modules, with a persistent left-hand navigation and global search.

•    The Kanban pipeline, calendar, and dashboard views shall support drag-and-drop interaction.

•    All list views shall support column configuration, sorting, filtering, and saved views.

•    The public property portal shall be a distinct, brandable interface separate from the internal application UI.

## **4.2 Hardware Interfaces**

•    The mobile app shall access device camera (property photos), GPS (viewing check-in **and** mandatory sales-officer field tracking sessions), and push-notification services.

•    No proprietary hardware is required; the System is designed for standard desktop/laptop/tablet/smartphone devices.

## **4.3 Software Interfaces**

| External System | Purpose | Interface Type |
| :---- | :---- | :---- |
| MLS / IDX Providers | Import listing data; push status updates | REST/SOAP/RETS or provider-specific API |
| Property Portals (Zillow, Realtor.com, PropertyFinder, Bayut, etc.) | Syndicate listings; import leads | API / XML feed |
| E-Signature (DocuSign, Dotloop, or native) | Send documents for signature; receive completion status | REST API \+ Webhook |
| Email (Gmail/Outlook/SMTP-IMAP) | Two-way email sync logged to CRM | OAuth2 / IMAP-SMTP |
| SMS / WhatsApp Gateway | Send/receive text messages | REST API |
| Telephony / VoIP Provider | Click-to-call, call recording | REST API / SIP |
| Accounting / ERP System | Export invoices, commissions, payments | REST API / File export |
| Calendar (Google/Outlook) | Two-way calendar sync | OAuth2 CalDAV/Graph API |
| Payment Gateway | Online rent/deposit/booking payments | REST API |
| Social/Ad Platforms (Meta, Google Ads) | Lead ads capture, campaign ROI | REST API |

## **4.4 Communication Interfaces**

•    All client-server communication shall use HTTPS (TLS 1.2+).

•    Real-time updates (e.g., new lead notification, chat) shall use WebSocket or equivalent push technology.

•    Webhooks shall use signed HTTPS POST payloads with retry-on-failure logic.

# **5\. Non-Functional Requirements**

## **5.1 Performance**

•    The System shall load any primary list/dashboard view within 2 seconds under normal load (P95).

•    The System shall support concurrent company users without performance degradation under expected load, scaling vertically or horizontally as needed.

•    Search across contacts/properties shall return results within 1 second for databases of up to 1,000,000 records.

## **5.2 Scalability & Availability**

•    The System shall be designed for horizontal scaling of application and database tiers.

•    The System shall target 99.9% uptime (excluding scheduled maintenance), with status communicated via a public status page.

•    The System shall support automated daily backups with point-in-time recovery.

## **5.3 Security**

•    All data in transit shall be encrypted (TLS 1.2+) and data at rest shall be encrypted (AES-256).

•    The System shall enforce strong password policy, optional multi-factor authentication (MFA), and session timeout controls.

•    The System shall enforce role-based and record-level access control so users only see data within their authorized scope inside the company, including **rental-tenant / portal-client isolation** (one client cannot access another client’s private data).

•    The System shall maintain a full audit trail for sensitive actions (login, data export, permission changes, document access, GPS track access).

•    The System shall undergo periodic third-party penetration testing and vulnerability scanning.

## **5.4 Usability & Accessibility**

•    The System shall conform to WCAG 2.1 AA accessibility guidelines for core workflows.

•    New users shall be able to complete core tasks (add lead, add property, move a deal stage) without training, guided by in-app onboarding tooltips.

•    The System shall support at least English plus configurable additional languages via a translation management interface.

## **5.5 Compliance & Data Privacy**

•    The System shall support GDPR data-subject rights (access, rectification, erasure, portability) and equivalent regional privacy regulations.

•    The System shall support configurable data retention and deletion policies for the company.

•    The System shall provide consent tracking for marketing communications (email/SMS opt-in/opt-out) and honor unsubscribe requests immediately.

## **5.6 Maintainability & Extensibility**

•    The System shall be built on a modular architecture allowing individual modules (e.g., Rental Management) to be enabled/disabled by company configuration.

•    The System shall support custom fields, workflows, and templates configurable by an administrator without vendor involvement.

•    The codebase shall maintain automated test coverage for core business logic and CI/CD pipelines for safe, frequent releases.

## **5.7 Adoption, Training & Data Migration**

Since a large share of CRM implementations fail to deliver value not because of missing features but because of poor user adoption and messy rollout, the System shall reduce that risk directly:

•    The System shall provide guided, tool-assisted data-migration utilities (CSV/Excel import with field mapping, validation, and duplicate-detection) for onboarding data from spreadsheets or legacy systems.

•    The System shall provide role-specific onboarding checklists, in-app guided tours, and a searchable help center to accelerate day-one adoption.

•    The System shall support a phased rollout model (see Section 10\) so customers can adopt core CRM first and add advanced modules once the team is comfortable, rather than a single disruptive 'big-bang' cutover.

•    The vendor/implementation team shall provide training materials (video, documentation) and a dedicated support channel during onboarding.

# **6\. Proposed System Architecture**

The following reference architecture is a recommendation, not a rigid mandate; the engineering team may adapt it during design so long as the non-functional requirements in Section 5 are met.

## **6.1 High-Level Architecture**

•    Presentation Layer: Responsive single-page web application (e.g., React/Vue/Angular) consuming a REST/GraphQL API; native mobile apps (iOS/Android) built on the same API.

•    Application/API Layer: Modular backend services (e.g., Node.js, Python/Django, or Java/Spring) exposing REST APIs, organized by domain module (Leads, Contacts, Properties, Deals, Leases, Billing, Documents, Communications).

•    Integration Layer: Dedicated connector services for MLS/IDX, portals, e-signature, SMS/email/telephony, and payment gateways, isolated from core business logic.

•    Data Layer: Relational database (e.g., PostgreSQL) for transactional data with role-/ownership-based row-level access scoping; object storage (e.g., S3-compatible) for documents/media; search index (e.g., Elasticsearch/OpenSearch, or Postgres full-text at company scale) for fast property/contact search; cache layer (e.g., Redis) for session and hot-data caching.

•    Messaging/Queue Layer: Asynchronous message queue (e.g., RabbitMQ/Kafka) for background jobs — email/SMS sending, feed imports, report generation, webhook delivery.

•    Infrastructure: Containerized deployment (Docker/Kubernetes) on a public cloud provider, with CDN for static assets and the public property portal, auto-scaling, and multi-region deployment for data residency needs.

## **6.2 Suggested Technology Stack (Illustrative)**

| Layer | Suggested Technology |
| :---- | :---- |
| Frontend (Web) | React or Vue.js, Tailwind CSS, responsive PWA |
| Mobile | React Native or Flutter (shared codebase, native performance) |
| Backend / API | Node.js (NestJS) or Python (Django/FastAPI) |
| Database | PostgreSQL (primary), Redis (cache/session) |
| Search | Elasticsearch / OpenSearch |
| File / Media Storage | S3-compatible object storage \+ CDN |
| Messaging Queue | RabbitMQ or Kafka |
| Hosting | AWS / Azure / GCP, Kubernetes-orchestrated containers |
| Communication Integrations | Twilio (SMS/WhatsApp/Voice), SendGrid/SES (email) |
| E-signature | DocuSign API or Dotloop API |

# **7\. Core Data Model (Key Entities)**

The entities below represent the core objects of the System. Relationships are indicative; the detailed entity-relationship diagram will be produced during technical design.

| Entity | Description |
| :---- | :---- |
| Contact | A person or organization: buyer, seller, tenant, landlord, investor, vendor. Can hold multiple roles. |
| Lead / Inquiry | An initial expression of interest, linked to a Contact and optional Property criteria. |
| Opportunity / Deal | A qualified pipeline entry linked to Contact(s), Property, pipeline, stage, and value. |
| Property / Listing | A property record: attributes, media, status, owner, and linked project/unit hierarchy. |
| Project / Development | Parent grouping for multi-unit developments (off-plan sales). |
| Lease / Rental Contract | Agreement between landlord (owner) and tenant for a specific unit, with billing schedule. |
| Transaction | A closed sale or signed lease, linked to invoices, commissions, and closing checklist. |
| Invoice / Payment | Financial record linked to a Transaction or Lease billing schedule. |
| Commission | Calculated payout linked to a Transaction, Agent(s), and split rules. |
| Document | File or e-signature envelope linked to Contact, Property, or Transaction. |
| Activity / Task | Calls, meetings, viewings, and to-dos linked to Contact, Property, or Deal. |
| Campaign | Marketing campaign (email/SMS/landing page) with linked Leads and performance metrics. |
| Maintenance Request | Service ticket linked to a Property/Lease and a Vendor. |
| User / Team / Branch / Company | Organizational hierarchy and access-control scope. |

# **8\. Primary Use Cases**

| ID | Use Case | Primary Actor(s) | Outcome |
| :---- | :---- | :---- | :---- |
| UC-01 | Capture and qualify a new lead | Agent / System | Lead is captured (manually or via integration), scored, and routed to an agent. |
| UC-02 | Match a lead to available properties | Agent | System suggests properties matching lead criteria; agent shares shortlist. |
| UC-03 | Schedule and record a property viewing | Agent, Buyer | Viewing is booked on calendar; feedback captured after the visit. |
| UC-04 | Progress a deal through the sales pipeline | Agent, Manager | Opportunity moves through stages to Won/Lost with required data at each stage. |
| UC-05 | Generate and send an offer/contract for e-signature | Agent, Buyer/Seller | Document generated from template, sent for signature, status tracked. |
| UC-06 | Close a transaction and calculate commission | Agent, Finance | Deal marked Won; invoice and commission auto-calculated per split rules. |
| UC-07 | Create and manage a rental lease | Property Manager, Tenant | Lease created; recurring invoices generated automatically per schedule. |
| UC-08 | Submit and resolve a maintenance request | Tenant, Property Manager, Vendor | Ticket logged via portal, assigned, tracked to completion. |
| UC-09 | Run a marketing drip campaign | Marketing Staff | Segmented list targeted with scheduled email/SMS sequence; engagement tracked. |
| UC-10 | View performance dashboard and export report | Manager, Owner | Role-based KPIs displayed; report scheduled or exported. |
| UC-11 | Sync listings with MLS/IDX and portals | Admin, System | Listings imported/pushed on schedule; errors logged and alerted. |
| UC-12 | Configure roles, permissions, and custom fields | Admin | New role or field created and applied without code deployment. |

# **9\. Core Business Workflow**

The diagram below summarizes the end-to-end flow that the modules in Section 3 support, from first contact through to post-sale/lease servicing. Every stage below is logged automatically against the relevant Contact, Property, and Deal records so no handoff loses information.

**Inquiry  →  Lead  →  Agent Assignment  →  Qualification / Follow-up  →  Property Match & Site Visit  →  Offer / Negotiation  →  Deal Won  →  Contract & E-Signature  →  Invoicing / Payment  →  Commission Payout  →  Post-Sale / Lease Servicing (Maintenance, Renewal)**

For rental/lease business lines, the flow branches after 'Deal Won' into: Lease Contract → Recurring Invoicing → Renewal or Termination → (if applicable) Maintenance Requests, rather than a one-time closing.

# **10\. Implementation Roadmap & MVP Phasing**

Rather than a single disruptive 'big-bang' launch, the System should be delivered and adopted in phases. This reduces adoption risk (Section 5.7) and lets core value be realized before advanced modules are rolled out.

### **Phase 1 — MVP: Foundation**

•    Property listing management (Section 3.3, core fields and media)

•    Lead capture and centralized contact database (Sections 3.1–3.2)

•    Basic sales/leasing pipeline (Kanban) (Section 3.4)

•    Automated lead assignment/routing rules

•    Core agent and admin dashboards

### **Phase 2 — Automation & Connectivity**

•    Automated follow-up reminders, nurture sequences, and first communication channel integration (email/SMS)

•    Calendar integration and site-visit/booking scheduling (Section 3.12)

•    Rental & lease management with recurring invoicing (Section 3.5)

•    Contract generation and document management (Section 3.7)

•    Integration of the CRM with property inventory/listing status across channels

### **Phase 3 — Financial Depth & Compliance**

•    Commission calculation and payout workflow (Section 3.6)

•    Invoicing, tax/VAT handling, and accounting-system integration

•    E-signature integration and legal template library

•    Advanced reporting, forecasting, and management dashboards (Section 3.13)

### **Phase 4 — Enterprise & Advanced Automation**

•    Multi-branch, multi-currency support (Section 3.15)

•    Customer/tenant/landlord self-service portal (Section 3.11)

•    AI-assisted lead scoring, next-best-action, and chatbot intake (Section 3.20)

•    MLS/IDX two-way sync and portal syndication at scale (Section 3.10)

•    Native mobile apps (iOS/Android) with offline support (Section 3.16)

•    Virtual tours (3D/VR) and advanced marketing automation

# **11\. Appendix**

## **11.1 Assumptions Summary**

•    This SRS is a functional baseline; detailed UI/UX wireframes and API contracts will be produced in subsequent design documents.

•    Which modules the company enables (e.g., leasing, marketing) is a configuration decision outside the pricing scope of this document.

•    Country-specific legal/compliance requirements (e.g., RERA in the UAE, TREC in the US) should be reviewed with local legal counsel and may add requirements to Sections 3.6 and 5.5.

## **11.2 Revision History**

| Version | Date | Description | Author |
| :---- | :---- | :---- | :---- |
| 1.0 | 06-Jul-2026 | Initial draft SRS compiled from market & Odoo research | Prepared with Claude (Anthropic) |
| 2.0 | 06-Jul-2026 | Consolidated with two supplementary AI-generated SRS drafts: added Objectives, Key Challenges Addressed, duplicate detection, internal team chat, tax/VAT handling, Adoption/Training/Data Migration, Core Business Workflow diagram, and phased Implementation Roadmap (MVP → Enterprise) | Prepared with Claude (Anthropic) |
| 2.1 | 12-Aug-2026 | Admin mandatory alignment: Super Admin highest authority; registration hierarchy Manager → Broker/Owner → Agent; rental-tenant/portal-client isolation (not SaaS multi-tenant); portal access only after completed contract; mandatory Property Manager per property; sales-officer GPS field tracking; lead de-dup by property + lead type | Project update |

   

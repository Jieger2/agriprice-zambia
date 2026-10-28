# Stakeholders, roles and use cases

This is the single map of **who uses AgriPrice Zambia, for what, through which channel, and where it is implemented and tested**. Each use case has an ID (UC-x.y), which is used in tests and review comments.

## Actors

| Actor | Kind | Identified by | Channel | Role in the system |
|---|---|---|---|---|
| **Farmer** (smallholder) | Person | Phone number (network-supplied via Africa's Talking); pseudonymised key `FARMER#<hmac>` | USSD `*384*1#`, SMS keywords | Sells crops, checks prices, receives match and price-alert SMS |
| **Buyer** (trader, aggregator, processor) | Person | Cognito user in group `buyers` (web), or phone number (USSD option 7) | Web app (Buy tab), USSD | Posts demand, finds supply, is matched with sellers |
| **Extension officer / cooperative** | Person | Cognito user in group `officers` | Web app (Officer tab) | Registers and supports farmers, reports field prices, monitors the region |
| **Administrator** | Person | Cognito user in group `admins` (also has every other role) | Web app (Admin tab) | Manages reference data, price sources, users and roles; watches platform activity |
| **New sign-up (no role)** | Person | Cognito user in no group | Web app | Can view public pages and waits for an administrator to assign a role |
| **Public visitor** | Person | none | Web app | Prices and Trends pages (read-only) |
| Price sources (WFP/HDX CSV, seed file, officer field reports) | External system | Source `id` in `CONFIG#sources` | HTTPS / S3 | Supply price observations to Ingestion |
| SMS/USSD gateway (Africa's Talking) | External system | Callback token `?k=` + optional IP allow-list | HTTPS callbacks / REST | Carries USSD sessions and SMS both ways (simulated locally) |
| Weather provider (Open-Meteo) | External system | none (free API) | HTTPS | Rainfall signal for forecasts (simulated locally) |
| Scheduler (EventBridge rules) | System | n/a | Time-based | Triggers ingestion every 6 h and analytics nightly |
| Platform team (DevOps & Security, Observability) | Operators | AWS IAM via GitHub OIDC | CI/CD, CloudWatch | Deploy, monitor, respond to alarms (`docs/runbooks.md`) |

## Entity relationships

```
Province 1─* Market 1─* Farmer 1─* Listing (SUPPLY)        Farmer 1─* PriceAlert ─* Market/Crop
                │                    │                            │
                │                    └──* Match *──1 Demand *──1 Buyer (Cognito user or USSD phone)
                │
                └─* CurrentPrice (per Crop) ◀── PriceObservation ◀── PriceSource (WFP | seed | FieldReport ◀── Officer)
Officer 1─* Farmer (registered_by, GSI1 OFFICER#)      Admin ─manages─▶ Market, Crop, Unit, PriceSource, User roles
Crop 1─* SeasonalSeries (per Province + national) ─▶ Forecast (monthly + 4 weekly, weather-adjusted)
```

How the links are stored (DynamoDB single table, see `docs/architecture.md`):

| Link | Stored as |
|---|---|
| Farmer → home market → province | `FARMER#<pid>.market`; market → province in the catalog |
| Officer → farmers | farmer `GSI1PK = OFFICER#<sub>` |
| Farmer → listings and alerts | each item's `GSI1PK = FARMERITEMS#<pid>` |
| Buyer → demands | demand `GSI1PK = OWNER#<sub>` (web) or `OWNER#ussd-<pid>` (USSD) |
| Demand ↔ listing | `MATCH#<demandId>` / `<listing SK>`, written once per pair |
| Alert → crop + market | `ALERT#<crop>#<market>`, fanned out by the Alerts service on price change |

## Use cases

### Farmer (USSD / SMS)
| ID | Use case | How | Implemented in | Tested in |
|---|---|---|---|---|
| UC-F1 | Register (province, nearest market, name) | `5` → province → market → name (`0` skips) | `ussd_app.route` | `test_ussd::test_register_then_check_prices` |
| UC-F2 | Check prices at the 3 nearest markets | `1` → crop | `price_lines` | `test_ussd::test_register_then_check_prices` |
| UC-F3 | Price by SMS | SMS `PRICE MAIZE` | `handle_sms` | `test_ussd::test_sms_keywords` |
| UC-F4 | Sell a crop (unit, quantity, available-from) | `2` → crop → kg/bags → qty → now/2 wk/1 mo → confirm | `route` (top 2) | `test_ussd::test_sell_flow_creates_listing_and_event` |
| UC-F5 | Set a price alert, above or below | `3` → crop → above/below → K per 50 kg | `route` (top 3) | `test_ussd::test_alert_flow_both_directions` |
| UC-F6 | Review and cancel my listings | `6` → `1` → pick | `my_account` | `test_stakeholders::test_farmer_my_account_lists_and_cancels` |
| UC-F7 | Review and remove my alerts | `6` → `2` → pick | `my_account` | same |
| UC-F8 | Change language (English, Bemba, Nyanja) | `4` → language | `route` (top 4) | `test_ussd::test_language_switch` |
| UC-F9 | Resume a dropped session (within 3 min) | redial → `1` | `handle_ussd` | `test_ussd::test_resume_after_dropped_session` |
| UC-F10 | Receive a buyer-match SMS | automatic | Matching service | `test_async_services::test_demand_event_notifies_buyer_and_farmers` |
| UC-F11 | Receive a price-alert SMS (max 1 per crop per day) | automatic | Alerts service | `test_async_services::test_alert_sent_once_per_day` |
| UC-F12 | Erase my data | SMS `STOP` removes profile, listings and alerts | `repo.forget_farmer` | `test_stakeholders::test_sms_stop_erases_profile_listings_and_alerts` |
| UC-F13 | Help | any other SMS | `handle_sms` | `test_ussd::test_sms_keywords` |

### Buyer
| ID | Use case | How | Implemented in | Tested in |
|---|---|---|---|---|
| UC-B1 | Sign in | Cognito hosted UI (local: role picker) | `app.js login` | browser walkthrough |
| UC-B2 | Post demand (crop, kg, max price, location, radius) | Buy → Post demand | `POST /api/demand` | `test_listings::test_demand_create_list_and_idempotency` |
| UC-B3 | Post demand from a feature phone | USSD `7` → crop → kg → max price → radius → confirm | `buy_crops` | `test_stakeholders::test_buyer_posts_demand_by_ussd_and_closes_it` |
| UC-B4 | See my demands with match counts | Buy → My demand | `GET /api/demand/mine` | `test_listings` |
| UC-B5 | See matched sellers and call them | open a demand | `GET /api/demand/<id>/matches` | `test_platform_features::test_matches_are_visible_to_the_owning_buyer_only` |
| UC-B6 | Find supply now within a radius | Buy → Find supply now | `GET /api/listings` | `test_listings::test_officer_registers_farmer_lists_crop_and_buyer_sees_it` |
| UC-B7 | Close a demand | web: Close button · USSD: `6` → `3` → pick | `DELETE /api/demand/<id>`, `my_account` | `test_stakeholders::test_web_buyer_closes_own_demand_only` |
| UC-B8 | Receive a match SMS | automatic | Matching service | `test_async_services::test_listing_event_picks_best_price` |
| UC-B9 | Work offline (queued writes, cached prices) | automatic | `app.js` outbox and delta sync | browser walkthrough |

### Extension officer / cooperative
| ID | Use case | How | Implemented in | Tested in |
|---|---|---|---|---|
| UC-O1 | Regional dashboard: every crop × market price and open supply | Officer → Regional dashboard | `GET /api/officer/board` | `test_stakeholders::test_board_shows_open_supply` |
| UC-O2 | Register a farmer on their behalf | Officer → Register farmer | `POST /api/farmers` | `test_listings` |
| UC-O3 | List a crop for a farmer (unit, available-from) | Officer → List crop for farmer | `POST /api/listings` | `test_platform_features::test_listing_with_unit_and_available_date` |
| UC-O4 | See my farmers with their listings and alerts | Officer → My farmers | `GET /api/officer/farmers` | `test_stakeholders::test_officer_manages_own_farmers` |
| UC-O5 | Cancel a listing or remove an alert for my farmer | My farmers → Cancel/Remove | `POST /api/officer/farmers/<pid>/remove` | same |
| UC-O6 | Set a price alert for my farmer | My farmers → Add alert | `POST /api/officer/farmers/<pid>/alerts` | same |
| UC-O7 | Submit a field price report (becomes a price source) | Officer → Field price report | `POST /api/field-prices` → S3 inbox → Ingestion | `test_data_services::test_inbox_source_archives_reports` |
| UC-O8 | View seasonal trends and the 4-week forecast | Trends tab | `GET /analytics/series` | `test_platform_features::test_weekly_forecast_with_weather` |

### Administrator
| ID | Use case | How | Implemented in | Tested in |
|---|---|---|---|---|
| UC-A1 | Platform overview (totals and last 7 days) | Admin → Overview | `GET /api/admin/overview` | `test_stakeholders::test_admin_overview_counts_activity` |
| UC-A2 | Add, edit or disable markets | Admin → Markets | `PUT /api/admin/catalog` | `test_platform_features::test_admin_catalog_round_trip_changes_every_service` |
| UC-A3 | Add or disable crops and their source names | Admin → Crops & units | same | same |
| UC-A4 | Add unit conversions | Admin → Crops & units | same | same |
| UC-A5 | Add, enable or disable price sources | Admin → Price sources | `PUT /api/admin/sources` | `test_platform_features::test_admin_sources_and_run_now` |
| UC-A6 | Run ingestion now | Admin → Price sources | `POST /api/admin/ingest` | same |
| UC-A7 | Invite a user with a role | Admin → Users & roles | `POST /api/admin/users` | `test_platform_features::test_admin_invites_lists_and_changes_roles` |
| UC-A8 | Grant or revoke roles; approve new sign-ups | tick boxes | `POST /api/admin/users/<u>/roles` | same |
| UC-A9 | Act as any role (support) | admins see every tab | `_require` lets admins through | `test_stakeholders::test_officer_manages_own_farmers` |

### Public visitor and new sign-up
| ID | Use case | How | Implemented in |
|---|---|---|---|
| UC-P1 | View current prices near a market, offline-tolerant | Prices tab | `GET /api/prices` (delta sync, ETag) |
| UC-P2 | View trends, seasonal index and forecast | Trends tab | `GET /analytics/series` |
| UC-P3 | New sign-up waits for a role | banner after sign-in | `app.js renderAuth` |

### System actors
| ID | Use case | Trigger | Implemented in |
|---|---|---|---|
| UC-S1 | Ingest, normalise and store prices from every enabled source | schedule (6 h) or UC-A6 | `ingestion_app` |
| UC-S2 | Publish price changes, then alert subscribers | after UC-S1 | SQS `price-normalised` → `alerts_app` |
| UC-S3 | Match supply and demand within radius, notify once per pair | `ListingCreated`, `DemandPosted` | EventBridge → SQS → `matching_app` |
| UC-S4 | Rebuild seasonal series and forecasts, adjusted for weather | nightly | `analytics_app.refresh` |
| UC-S5 | Count platform activity for the overview | every business write | `repo.bump` |

## Role → permission matrix (enforced in code and by the API Gateway Cognito authoriser)

| Endpoint / menu | Public | Farmer (phone) | Buyer | Officer | Admin |
|---|:-:|:-:|:-:|:-:|:-:|
| Prices, Trends, markets | ✓ | ✓ (USSD/SMS) | ✓ | ✓ | ✓ |
| USSD menus 1–7 | | ✓ | ✓ (option 7) | | |
| Post/close demand, my demand, matches, find supply | | | ✓ | find supply only | ✓ |
| Board, register farmer, list for farmer, my farmers, field price | | | | ✓ | ✓ |
| Manage another officer's farmer | | | | ✗ | ✓ |
| Admin overview, catalog, sources, ingestion, users | | | | | ✓ |

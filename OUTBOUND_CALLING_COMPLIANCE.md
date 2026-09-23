# Jurisdiction-specific outbound deployment decisions

These recipes are technical examples, not legal advice or a certification of regulatory
compliance. Before deployment, assess applicable laws and provider requirements for each
jurisdiction, recipient, and call purpose with qualified counsel. Example settings and passing
tests do not establish legal compliance or eliminate liability. This guidance does not modify
the [Apache License 2.0](LICENSE), impose additional conditions on its grant, or replace any
separately accepted terms for hosted services.

## Approve a bounded deployment, not worldwide calling

Treat each combination of caller/operator jurisdiction, recipient jurisdiction, and call purpose
as a separate deployment decision. Include relevant states, provinces, or other local rules;
the location of the server or the caller ID alone does not establish every applicable rule.
Global technical availability is not authorization to call every region.

The recommended deployment policy is to leave unreviewed combinations disabled. A missing,
expired, or unresolved decision should block launch in the deploying application's eligibility
checks. This is an operator policy to implement, **not a gate already implemented by this
recipe**. The CLI does not read this document or enforce legal sign-off.

Keep the initial scope narrow: one reviewed use case and a small set of destinations. Review a
new country, subnational region, recipient category, or call purpose before enabling it. Do not
assume that permission for an appointment reminder also covers marketing, lead generation,
political calls, fundraising, debt collection, or sensitive-sector calls.

## Decision record for each deployment profile

Keep this record in your controlled deployment/compliance system, not in public source control.
Use references to evidence rather than copying phone numbers, consent records, or transcripts
into the repository or PR. Start with `status: blocked`; approval is an internal deployment
decision, not a regulator's certification.

| Decision | What the deployment owner must record and resolve |
| --- | --- |
| Identity and scope | Profile ID/version; responsible legal entity; caller/operator locations; recipient countries and relevant subnational regions; consumer/business audience; approved routes and excluded destinations. |
| Call classification | Actual script and purpose: purely transactional reminder, sales/marketing, or mixed content; whether artificial/AI voice, automation, sector-specific rules, or recipient type changes the requirements. Do not classify by the recipe's name alone. |
| Legal and provider review | Applicable laws, official guidance, and carrier/provider conditions; dated source links; qualified reviewer; conclusions, conditions, and unresolved questions. Document any relied-on exception and its limits. |
| Permission to contact | What authorization or consent is required for this purpose, channel, voice type, and caller; how evidence is verified; its scope and continuing validity; treatment of third-party lists, reassigned numbers, withdrawal, and uncertainty. |
| Suppression and opt-out | Applicable national/local registries and internal lists; freshness and screening rules; required opt-out mechanisms; propagation across systems; owner and process for honoring requests. |
| Time and frequency | Recipient-local permitted hours, days, holidays, and any frequency limits; daylight-saving handling; reliable timezone/location inputs; behavior when the location or timezone is uncertain. |
| Caller identity and disclosures | Authorized caller ID and callback/contact details; business and purpose identification; any required AI/automated-call, recording, or other notices; approved language and script version. |
| Audio and personal data | Whether recording/transcription is allowed and on what basis; privacy notices; data-processing roles and agreements; processors and locations; transfers, retention, deletion, and access controls. Recording off does not stop audio/transcript processing. |
| Delivery and failure behavior | Whether voicemail, AMD, transfers, and follow-up callbacks are in scope; handling of misclassification and uncertain outcomes; any required immediate opt-out/termination behavior. Review features before enabling them. |
| Enforcement and evidence | Application-side checks and provider settings implementing the decision; test evidence; complaint/abuse contact; ability to pause calling; consent/suppression audit references protected as sensitive data. |
| Approval and change control | Status (`blocked` or `approved`), named accountable approver and legal reviewer, approval date, next review date, configuration/script version, and triggers for re-review or suspension. |

An unresolved row is not implicitly approved. Record the specific resolution or a justified,
reviewed determination that it is not applicable. For cross-border calls, assess the relevant
origin and destination requirements together rather than choosing whichever is less restrictive.

## Map the decision to what this recipe actually checks

The current [outbound recipe](examples/outbound_call/README.md) supplies useful building blocks,
but it is not a jurisdiction rules engine:

- `OUTBOUND_ALLOWED_COUNTRIES` checks the region inferred from the telephone number. It does
  not verify the recipient's physical location or state/province, or select applicable law.
  Its `US` default is an example, not an approved US deployment profile.
- `recipient_timezone` is supplied by the application. The recipe validates an IANA timezone
  name, not evidence of where the recipient is located.
- `OUTBOUND_CALLING_WINDOW_START` and `OUTBOUND_CALLING_WINDOW_END` define one daily window for
  the configured deployment. The default 09:00–20:00 is not a universal legal safe harbor.
  There is no per-jurisdiction schedule, holiday/day-of-week rule, or recipient-frequency policy.
- `consent_reference` is a non-empty audit reference; it does not verify consent or an exception.
  Resolve it against authoritative evidence before launch.
- The SQLite suppression list captures this recipe's wrong-number and opt-out results. It does
  not query external registries, customer-wide lists, or other deployments. Integrate those
  sources and propagate withdrawals through the adopting application.
- The automation disclosure, conversational opt-out tool, human-only AMD default, and disabled
  Twilio recording are example behavior. They do not prove that all required disclosures,
  opt-out mechanisms, voicemail restrictions, or data-processing obligations are satisfied.
- A successful preflight, `--place-call`, or a passed eval is not jurisdictional authorization.
  The flag acknowledges an external call; it does not establish legal compliance.

Do not enable several regions simply by widening the country list. Where approved profiles
require different behavior, enforce profile-specific rules in the application before
`calls.create`; do not try to resolve those rules only through the model prompt. This recipe
does not load deployment-profile IDs or decision records from `call.json`.

## Official starting points for local review

Reference links reviewed on 23 September 2026. These are starting points, not a complete legal
checklist or approval of any region. Check current sources at deployment and on re-review.

- **United States:** review [FCC TCPA/AI-voice guidance](https://docs.fcc.gov/public/attachments/FCC-24-17A1.pdf)
  and the [FTC Telemarketing Sales Rule guidance](https://www.ftc.gov/business-guidance/resources/complying-telemarketing-sales-rule),
  plus relevant state requirements. Determine the effect of the specific call purpose, number
  type, consent, and any applicable exception; do not assume a one-call CLI escapes voice rules.
- **United Kingdom:** use the [ICO's telephone-marketing guidance](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guide-to-pecr/electronic-and-telephone-marketing/).
  Assess the actual content, automated/AI delivery, recipient category, consent, and privacy
  requirements; do not assume consent for live marketing also covers automated marketing.
- **EU/EEA destinations:** start with [ePrivacy Directive Article 13](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32002L0058)
  and identify the destination's applicable national implementation and regulator guidance.
  Review relevant data-protection and AI-transparency requirements too; one regional entry does
  not approve every country or use case.
- **Canada:** review the [CRTC Unsolicited Telecommunications Rules](https://crtc.gc.ca/eng/trules-reglest.htm),
  including the rules applicable to automatic dialing/announcing devices, consent, and do-not-call
  obligations. Classify the proposed message and any claimed exemption separately.
- **Australia:** start with the [ACMA Do Not Call Register guidance](https://www.acma.gov.au/do-not-call-register)
  and follow the applicable telemarketing/research-call standards and privacy requirements for
  the proposed use case.
- **All other destinations:** identify the relevant telecoms and privacy authorities and current
  national/local requirements before approval. Absence from this list is not permission to call.
- **Provider requirements:** review the [Twilio Voice Services Policy](https://www.twilio.com/en-us/legal/service-country-specific-terms/voice-sip),
  [Acceptable Use Policy](https://www.twilio.com/en-us/legal/aup), and applicable country-specific
  conditions in addition to applicable law and any separately accepted Dialt service terms.

## Deployment verification and re-review

Before a controlled live test, approve the bounded test profile and use an explicitly opted-in
destination controlled by the tester with synthetic appointment facts. Do not use a real call
to find out whether a recipient or jurisdiction should have been blocked.

Before production, exercise the adopting application's eligibility checks with mocked provider
calls. Verify that unknown/unapproved profiles, missing consent evidence, withdrawn permission,
suppressed destinations, and disallowed local times/days cannot reach `calls.create`. Test
timezone/daylight-saving boundaries, cross-border routes, script/profile mismatches, required
disclosures and opt-out behavior, and persistence across process restarts. Distinguish checks
already covered by this recipe's offline tests from controls your application still must build.

Keep sanitized test evidence with the approved profile; publish no personal data or credentials.
Assign an owner to complaints and suspension decisions. Re-review when laws/provider policies,
jurisdictions, scripts or purposes, voice/recording behavior, or data-processing arrangements
change, and when incidents show a safeguard may not work. Pause affected deployments while
material uncertainties are unresolved.

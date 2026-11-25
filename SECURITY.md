# Sikkerhet

familiekilder-mcp bruker innloggingene dine hos skole, barnehage og fritid og
leser det tjenestene sender familien. En sikkerhetsfeil kan derfor gi andre
tilgang til opplysninger om barn. Meld den privat, ikke i en offentlig sak.

## Slik melder du fra

Bruk GitHubs private rapportering: Åpne fanen **Security** på
[reposiden](https://github.com/familiekilder/familiekilder-mcp) og velg
**Report a vulnerability**. Rapporten kan bare du og vedlikeholderne se.

Prosjektet har ingen e-postadresse for sikkerhet.

Fortell:

- hva som er galt, og hva det kan føre til;
- hvordan feilen kan gjentas, helst med oppdiktede data;
- versjonen (`familiekilder-mcp --version`), operativsystemet og om serveren
  kjører på datamaskinen eller på Fly.io.

## Hva som er en sikkerhetsfeil

Meld privat om alt som kan:

- lekke en innlogging, et passord, et token eller en økt, for eksempel til
  logger, feilmeldinger, filer andre kan lese, eller til andre enn tjenesten;
- la andre enn deg lese familiens beskjeder, aktiviteter eller vedlegg, for
  eksempel fra serveren din på Fly.io;
- få serveren til å endre noe hos en tjeneste: sende, svare, melde på, slette
  eller merke noe som lest. Serveren skal bare lese.

Dette er ikke sikkerhetsfeil. Meld dem som en vanlig sak under **Issues**:

- en kobling som har sluttet å virke, eller en innlogging som er utløpt;
- feil i selve tjenesten, som Spond eller Vigilo. Si fra til tjenesten.

At det assistenten leser, sendes til AI-leverandøren du bruker, er slik
programmet virker, og ingen feil.

## Sikkerhetsgrenser for det lokale oppsettet

`familiekilder-mcp setup` kjører en lokal webveiviser på `http://127.0.0.1:28417`
og åpner den statiske oppsettssiden
`https://familiekilder.github.io/familiekilder-mcp/oppsett/` på GitHub Pages.
GitHub ser IP-adressen din når nettleseren henter den statiske siden; siden
laster ingen eksterne ressurser, lagrer ingenting og sender bare en
`GET`-forespørsel til `http://127.0.0.1:28417/status` når du klikker.

Slik er grensen mellom nettleseren, oppsettssiden og den lokale veiviseren satt:

- **Statusrute (`GET /status`).** Eksponerer bare
  `{"app": "familiekilder-mcp", "protocol": 1}` — ingen profil, datamappe,
  tjenestestatus, innlogging eller token. Krever nøyaktig `Host`, tillater
  `Origin` bare fra `https://familiekilder.github.io` (nøyaktig opprinnelse,
  aldri `*` og aldri credentials) eller veiviseren selv, og avviser
  `X-Setup-Instance` dersom `Origin` er til stede.
- **Navigasjonsunntak på `GET /`.** Toppnivå-navigasjon uten `Origin`
  (`Sec-Fetch-Site: cross-site`, `Sec-Fetch-Mode: navigate`,
  `Sec-Fetch-Dest: document`) slipper gjennom til `/`, så lenken fra
  oppsettssiden virker også når nettleseren blokkerer lokal `fetch`. `/` svarer
  med `Cross-Origin-Opener-Policy: same-origin`, `X-Frame-Options: DENY` og
  `frame-ancestors 'none'`. Alle `/api/*`-kall krever per-prosess
  `X-Setup-Token`, riktig `Host`, og samme-opprinnelse `Origin` på `POST`.
- **Lås og markør.** Prosesslåsen `setup` og den private markøren
  `.private/setup-server.json` (port, tilfeldig kjøre-ID, protokoll og
  kodefingeravtrykk) gjør at en ny `setup`-kommando verifiserer og gjenbruker
  en kjørende veiviser for samme datamappe i stedet for å starte en ny.
- **Delt Pages-opprinnelse.** Alle GitHub Pages-sider under organisasjonen
  `familiekilder` deler opprinnelsen `https://familiekilder.github.io` og kan
  derfor lese `/status` (men ikke veiviseren eller `/api/*`). Organisasjonen
  publiserer ikke andre Pages-sider uten en sikkerhetsvurdering.
- **Andre OS-brukere på samme maskin.** En annen OS-bruker eller et lokalt
  program på samme maskin kan kontakte `127.0.0.1:28417` mens veiviseren
  kjører, eller binde porten først. Andre lokale OS-brukere regnes som betrodde
  i første versjon.
- **Ettklikksfelle (dobbeltklikkfelle).** Navigasjonsunntaket på `/` betyr at
  ethvert nettsted kan åpne `http://127.0.0.1:28417/` i en ny fane (uten å
  kunne lese innholdet) og i teorien lure brukeren til ett klikk der, for
  eksempel på «Slå av» eller «Avslutt oppsett». Innlogging krever dialog og
  eksplisitte avkryssinger. Denne restrisikoen er kjent og godtatt i første
  versjon.

## Del aldri hemmeligheter

Lim aldri inn tokens, øktfiler, callback-adresser fra innloggingen, passord,
navn på barn eller innhold fra ekte beskjeder, verken i saker, diskusjoner,
pull requests eller i den private rapporten. Beskriv feilen uten dem. Står noe
slikt i en feilmelding, bytt det ut med `…`.

Har du delt noe slikt ved et uhell, slett det med en gang. Bytt passordet hos
tjenesten, eller trekk tilbake tokenet der du laget det.

## Versjoner som får rettinger

Bare nyeste kode på `main` får sikkerhetsrettinger. Eldre versjoner rettes ikke
for seg. Se hvilken versjon du har, med `familiekilder-mcp --version`.

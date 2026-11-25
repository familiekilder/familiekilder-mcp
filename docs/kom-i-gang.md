# Kom i gang med familiekilder-mcp

[Tilbake til forsiden](../README.md).

Denne veiledningen er for førstegangsoppsett og vedlikehold. Oppsettet krever
litt terminalbruk. Be gjerne om hjelp fra noen med teknisk erfaring.

## Installere

Programmet kan brukes på macOS, Windows og Linux. Du trenger Python 3.11 eller
nyere og en nettleser for innlogging: Brave, Google Chrome eller Microsoft
Edge; se [nettleseren](#nettleseren). Lesing av PDF krever Poppler
(`pdftotext`, `pdftoppm`), og Word-dokumenter krever LibreOffice.

### Hva er prøvd

- **Mac med Brave** er prøvd med ekte innlogging hos tjenestene.
- **Nytt:** Chrome og Edge på Mac, og alle tre nettleserne på Windows og
  Linux. Det er prøvd maskinelt, ikke med ekte innlogging ennå. På alle tre
  systemene kjører prøvene installasjonen ordrett etter denne veiledningen,
  veiviseren, serveren slik AI-appen starter den, nettleserne og
  innloggingsløpet mot oppdiktede, lokale sider. Lagrede passord er prøvd mot
  Legitimasjonsbehandling på Windows og GNOME Keyring på Linux. Ingen prøve
  logger inn hos en ekte tjeneste.
- **Fast oppsettsside:** Den faste oppsettssiden
  (`https://familiekilder.github.io/familiekilder-mcp/oppsett/`) er prøvd uten
  synlig vindu, med oppdiktet profil: Brave 156 og Chrome 157 på Mac, Brave 158
  på Ubuntu, og Edge 158 og Chrome 158 på Windows. Ikke prøvd ennå: den synlige
  tillatelsesdialogen, Safari, Firefox, Edge på Mac, og bruk med ekte innlogging
  på Windows og Linux.

Vil du prøve på Windows eller Linux, les [Prøve på Windows eller
Linux](prove-pa-windows-og-linux.md). Der står hva du trenger, hva du skal se
i hvert steg, hva hver maskinell prøve dekker, og hva du limer inn i en sak
hvis noe går galt. Si gjerne fra under **Issues**, også når det virker.

### Last ned og installer

Last ned kildekoden fra reposiden (**Code → Download ZIP**) og pakk den ut.

**macOS og Linux.** Åpne Terminal og gå til mappen du pakket ut; bytt ut stien
i første linje. Har stien mellomrom, setter du den i enkle anførselstegn. På
Debian og Ubuntu trenger du også pakken `python3-venv`.

```sh
cd /sti/til/familiekilder-mcp
scripts/bootstrap-local-venv
export PATH="$PWD/.private/venv/bin:$PATH"
```

Skriptet lager `.private/venv` og installerer pakken der, med versjonene som er
låst i `pyproject.toml`. Kommandoen er da
`.private/venv/bin/familiekilder-mcp`. `export`-linjen gjør kommandoen
tilgjengelig i dette terminalvinduet. Den gamle kommandoen `familiekilder`
virker også.

**Windows.** Installer Python 3.11 eller nyere fra
[python.org](https://www.python.org/downloads/windows/). Åpne PowerShell og gå
til mappen du pakket ut. Har stien mellomrom, setter du den i enkle
anførselstegn: `cd 'C:\sti med mellomrom\familiekilder-mcp'`.

```powershell
cd C:\sti\til\familiekilder-mcp
py scripts\setup.py
$env:Path = "$PWD\.private\venv\Scripts;$env:Path"
```

`scripts\setup.py` lager `.private\venv`, installerer pakken der og åpner
veiviseren. Kommandoen er da `.private\venv\Scripts\familiekilder-mcp.exe`.
Når du har avsluttet veiviseren, gjør den siste linjen kommandoen
tilgjengelig i dette vinduet.

### Hvor dataene ligger

Innlogginger, oppsett og mellomlager ligger i dataroten, utenfor koden:

| System | Standard datarot |
|---|---|
| macOS | `~/Library/Application Support/familiekilder-mcp` |
| Windows | `%LOCALAPPDATA%\familiekilder-mcp` |
| Linux | `~/.local/share/familiekilder-mcp`, eller `$XDG_DATA_HOME/familiekilder-mcp` |

`--root MAPPE` eller `FAMILIEKILDER_ROOT` velger en annen. Mappen `.private`
er bare tilgjengelig for brukeren din: på macOS og Linux med filrettigheter, på
Windows med en tilgangsliste. Mappen må også eies av deg. Har en annen bruker
på maskinen laget den, stopper programmet; flytt den bort eller velg en annen
datarot.

## Nettleseren

Innlogging i nettleser åpner en isolert, midlertidig profil i Brave, Google
Chrome eller Microsoft Edge. Profilen slettes etterpå, og den bruker ikke
bokmerker, innlogginger eller utvidelser fra din vanlige nettleser. Programmet
styrer vinduet gjennom en privat forbindelse, aldri gjennom en nettverksport.

Programmet bruker bare en nettleser som er installert fra leverandøren, og
kontrollerer den før hver start. En nettleser som ikke består kontrollen,
brukes ikke.

- **macOS:** Nettleseren må ligge i `/Applications` eller `~/Applications`.
  Programmet kontrollerer signaturen fra leverandøren og at macOS godtar appen.
- **Windows:** Installer nettleseren **for alle brukere** på maskinen, så den
  havner under `Program Files` eller `Program Files (x86)`. Det krever
  administrator. En nettleser som bare er installert for deg, under `AppData`,
  blir avvist. Det er vanlig for Chrome og Brave når de installeres uten
  administratorrettigheter. Programmet kontrollerer også signaturen fra
  leverandøren, og avviser en mappe som vanlige brukere kan endre.
- **Linux:** Bruk leverandørens egen .deb- eller .rpm-pakke fra
  [brave.com](https://brave.com/linux/),
  [google.com/chrome](https://www.google.com/chrome/) eller
  [microsoft.com/edge](https://www.microsoft.com/edge). Pakkene legger
  nettleseren under `/opt`. Programmet kontrollerer hele mappen: alle filer
  der må eies av root, ingen andre må kunne endre dem, og ingen lenker må peke
  ut av mappen. Snap, Flatpak og distribusjonens `chromium`
  støttes ikke. Innloggingen trenger et skrivebord med skjerm. Over SSH eller
  på en server uten skjerm stopper den med en melding.

Har du ikke valgt, bruker innloggingen den første av Brave, Chrome og Edge,
i den rekkefølgen, som består kontrollen. En nettleser som feiler kontrollen,
eller som er installert på en måte som ikke støttes (bare for deg under
`AppData` på Windows; Snap, Flatpak eller distribusjonens Chromium på Linux),
hoppes over. Du får en linje om hvilken som ble hoppet over og hvorfor. Har du
valgt en nettleser, prøves bare installasjoner av den nettleseren, i samme
rekkefølge som ved automatisk valg. Hvis ingen av dem består, sier
feilmeldingen hvorfor og hva du kan gjøre. Velg i veiviseren under «Nettleser for
innlogging», eller fra terminalen:

```sh
familiekilder-mcp browser            # vis installerte nettlesere og valget
familiekilder-mcp browser chrome     # bruk Chrome; også brave, edge eller auto
```

`auto` fjerner valget. Valget gjelder profilen du bruker. Med
`--browser brave|chrome|edge` på `auth … browser-login` og
`auth … browser-bootstrap` velger du for én innlogging.

### Forhåndssjekk

```sh
familiekilder-mcp doctor           # rapport til skjermen
familiekilder-mcp doctor --json    # det samme som JSON
```

Forhåndssjekken bruker lokale opplysninger. Rapporten viser nettleserne,
passordlagring, statusen til den private datamappen, AI-klientene, skjerm på
Linux og hvilke tjenester som er slått på. For dataroten vises bare hvordan
den ble valgt og statusen, også ved feil. Profilnavn og egendefinerte stier
utelates. Den samme rapporten står under «Hva trenger jeg på maskinen?» i
veiviseren og kan limes inn i en sak.

- **macOS:** sjekker appstien og leser app- og utgiver-ID fra signaturen
  med `codesign -dv`. Gatekeeper (`spctl`) kjøres ved innlogging, før
  nettleseren startes.
- **Windows:** sjekker programstien, lenker og skrivetilgang med lokale
  tilgangslister. Authenticode-signaturen og utgiveren sjekkes ved
  innlogging, før nettleseren startes.
- **Linux:** sjekker pakkestien, eier, rettigheter og lenker i hele
  nettleserpakken lokalt, som ved innlogging.

På Mac og Windows står det «KANDIDAT» ved første aktuelle installasjon og
«UAVKLART» ved andre aktuelle nettlesere. Innloggingen avgjør hvilken
installasjon som består signaturkontrollen. Avslutningskoden er 1 ved en
konkret feil som blokkerer innlogging.

## Oppsett i nettleseren

Kjør `familiekilder-mcp setup`, eller fra prosjektmappen
`python3 scripts/setup.py` (på Windows `py scripts\setup.py`). Skriptet
installerer pakken først. `scripts/setup` virker også på macOS og Linux.

Som standard starter veiviseren lokalt på `http://127.0.0.1:28417` (bare
tilgjengelig på denne maskinen) og åpner den faste oppsettssiden
`https://familiekilder.github.io/familiekilder-mcp/oppsett/` i
standardnettleseren din. Siden er statisk og hentes fra GitHub Pages (GitHub
ser da IP-adressen din, men siden sender oss ingenting). Når du klikker
**Sjekk tilkoblingen**, spør siden bare `http://127.0.0.1:28417/status` på din
egen maskin. Nettleseren kan spørre om tilgang til apper på enheten; velg
**Tillat**, og klikk **Fortsett oppsettet** for å åpne veiviseren. Innlogging
og lagring skjer bare i den lokale veiviseren. Bunnteksten i veiviseren viser
hvilken datamappe som er åpen (med `~` for hjemmemappen).

1. Velg «Logg inn» på tjenestene du bruker. Vigilo, Skolemelding, Spond og
   IST Home har innebygd oppsett, så du trenger ingen fil. Hoopit kobles til
   med et token du lager selv; se punkt 2. Flyt spør om kommune. Valgte du feil
   kommune, kan du velge en annen i innloggingsdialogen så lenge du ikke har
   logget inn. Se [klientoppsett](klientoppsett.md).
2. Logg inn i det separate nettleservinduet. BankID gjør du selv. For Spond
   merker veiviseren selv når du er logget inn, og lagrer innloggingen. Skjer
   det ikke noe når Spond er ferdig lastet, trykker du «Jeg er logget inn».
   Meldingen med knappen står øverst i veiviseren og blir stående synlig mens
   du venter. For Hoopit velger du «Koble til med token»:
   1. Åpne [tokensiden hos Hoopit](https://id.hoopit.io/security/access-tokens/)
      og logg inn.
   2. Skriv «familiekilder» i «Navn».
   3. Under «Dine» krysser du av for «Se aktivitetene og invitasjonene dine, og
      barnas», «Svare på invitasjoner for deg selv og barna dine» og «Se innlegg
      og kommentarer i gruppene dine». Boksene under «Som administrator» trengs
      ikke.
   4. Velg hvor lenge tokenet skal virke i «Utløper om». Når det har utløpt,
      lager du et nytt.
   5. Trykk «Opprett et token», og kopier tokenet. Det vises bare én gang.
   6. Lim det inn i veiviseren og velg «Kontroller og lagre».

   Veiviseren kontrollerer tokenet hos Hoopit og sier fra om det er utløpt
   eller mangler en tilgang. Se [Hoopit](hoopit-ingestion.md).
3. Velg Claude Desktop eller lokal ChatGPT / Codex. Veiviseren endrer bare
   oppføringen for familiekilder-mcp og beholder øvrige klientinnstillinger.
4. Test MCP-forbindelsen og start AI-klienten på nytt. Testen henter ingen
   beskjeder; den kontrollerer den lokale serveren og leseverktøyene.

**Avslutt oppsett** (eller Ctrl+C i terminalen) lukker bare selve
oppsettsveiviseren. Lagrede innlogginger og klientkoblinger beholdes, og
AI-appen starter MCP-serveren selv når du bruker den.

Valg til `familiekilder-mcp setup` (kan også gis til `scripts/setup.py` og
`scripts/setup`):

- `--local`: åpner den lokale veiviseren direkte (`http://127.0.0.1:28417`) i
  stedet for den faste oppsettssiden.
- `--no-open`: skriver adressene i terminalen uten å åpne nettleseren.
- `--port N`: binder til valgt lokal port `N` (1–65535). Er porten opptatt,
  stopper kommandoen med feil og kode 1. På en annen port enn `28417` åpnes den
  lokale veiviseren direkte, fordi oppsettssiden bare spør port `28417`.
- `--port 0`: velger en ledig port og åpner den lokale veiviseren direkte.

Claude Desktop-konfigurasjonen lagres i `claude_desktop_config.json` i
`~/Library/Application Support/Claude/` på macOS, `%APPDATA%\Claude\` på
Windows og `~/.config/Claude/` på Linux. Lokal ChatGPT / Codex bruker
`~/.codex/config.toml` på alle systemene. Har du valgt en annen
Codex-konfigurasjonsmappe, må du legge til serveren der manuelt.
Konfigurasjoner som ikke
kan leses sikkert, overskrives ikke. En lokal konfigurasjon betyr ikke at
vanlig ChatGPT-nettchat får tilgang til serveren.

## Kom tilbake til oppsettet

Oppsettssiden `https://familiekilder.github.io/familiekilder-mcp/oppsett/` er
fast og virker også etter at den lokale veiviseren er lukket. Hvis veiviseren
har stoppet, eller du vil legge til flere tjenester senere, starter du den
igjen fra det eksisterende miljøet uten å kjøre `scripts/setup.py` eller `pip`
på nytt.

### Åpne på nytt uten `pip`

Har du `familiekilder-mcp` i `PATH`, kjører du `familiekilder-mcp setup` (eller
`familiekilder-mcp setup --no-open` hvis oppsettssiden allerede er åpen i
nettleseren). Ellers kjører du Python i det eksisterende miljøet direkte (bytt
ut mappen hvis du la koden et annet sted enn `~/familiekilder-mcp`):

**macOS og Linux:**

```sh
~/familiekilder-mcp/.private/venv/bin/python -I -m familiekilder setup --no-open
```

**Windows (PowerShell):**

```powershell
& "$HOME\familiekilder-mcp\.private\venv\Scripts\python.exe" -I -m familiekilder setup --no-open
```

Brukte du `--root MAPPE` eller `--profile NAVN` sist, må flagget stå **før**
`setup`:

```sh
~/familiekilder-mcp/.private/venv/bin/python -I -m familiekilder --profile nabo setup --no-open
```

Fjern `--no-open` hvis du vil at kommandoen også skal åpne nettleseren. Klikk
deretter **Prøv igjen** i den åpne oppsettssidefanen. Har du en gammel
veiviserfane på port `28417`, merker den at en ny prosess har overtatt porten
(utløpt token) og viser knappen **Last siden på nytt**.

Kjører veiviseren allerede for samme datamappe og versjon, gjenbruker en ny
`setup`-kommando den kjørende veiviseren i stedet for å starte en ny prosess
(den skriver adressene og avslutter med kode 0). Er pakken oppdatert mens
veiviseren kjører, eller ber du om en annen `--port`, sier den fra og ber deg
avslutte den kjørende veiviseren først.

### Hva som overlever, og hva som må gjentas

- **Overlever:** Lagrede innlogginger og tokens, aktiverte tjenester, valgt
  nettleser for innlogging, valgt Flyt-kommune og klientkoblingene for Claude
  Desktop og Codex. Alt dette ligger på disk og vises med en gang når du åpner
  veiviseren igjen.
- **Må gjentas:** En innlogging som ble avbrutt før den ble lagret, må startes
  på nytt. Hvilket steg (1–3) du sto på i veiviseren og resultatet av **Test
  forbindelsen** huskes ikke mellom kjøringer; kjør testen på nytt i steg 3
  hvis du vil kontrollere forbindelsen igjen.

### Vanlige situasjoner

- **Avslått tillatelse i nettleseren.** Hvis du avviste forespørselen om lokal
  tilgang, melder oppsettssiden at lokal tilgang er blokkert for siden. Du kan
  likevel bruke **Åpne veiviseren direkte** (`http://127.0.0.1:28417/`) — en
  vanlig lenke trenger ikke den tillatelsen. Vil du at tilkoblingssjekken skal
  virke igjen, åpner du nettleserens nettstedsinnstillinger for oppsettssiden
  (vanligvis fra ikonet i adressefeltet eller under nettstedstillatelser i
  innstillingene), tillater tilgang til lokale apper/enheter, laster siden på
  nytt og klikker **Prøv igjen**.
- **Safari.** Ikke prøvd ennå, men ifølge en åpen feilsak hos WebKit (171934)
  blokkerer Safari tilkoblingssjekken fra en HTTPS-side til `http://127.0.0.1` som
  blandet innhold. Bruk lenken **Åpne veiviseren direkte** på oppsettssiden,
  gå rett til `http://127.0.0.1:28417/`, eller start med
  `familiekilder-mcp setup --local`.
- **Opptatt port.** Hvis et annet program allerede bruker port `28417` når du
  kjører `setup` uten `--port`, velger veiviseren automatisk en ledig port i
  stedet (på Windows prøver den port `28417` på nytt i opptil ti sekunder først
  for å tåle rask gjenåpning). Da åpner den den lokale adressen direkte og
  skriver i terminalen at oppsettssiden ikke når denne veiviseren, sammen med
  adressen du skal bruke. Oppgir du `--port N` og porten er opptatt, stopper
  kommandoen med feil og kode 1.
- **To profiler samtidig.** Bare én veiviser kan ha port `28417` om gangen.
  Starter du en ny profil samtidig (med `--profile` eller `--root`), får den
  andre profilen en ledig port og sin egen lokale adresse i terminalen. Bruk
  den adressen, og se på `Datamappe:` i bunnteksten i veiviseren for å være
  sikker på hvilken profil du har åpen.
- **Eldre installasjon uten fast port.** En eldre installasjon uten støtte for
  den faste porten velger alltid en tilfeldig port og har ingen statusrute på
  port `28417`. Bruk adressen som skrives i terminalen, eller oppdater
  installasjonen hvis du vil bruke den faste oppsettssiden.
- **GitHub Pages utilgjengelig eller uten nett.** Veiviseren kjører lokalt og
  avhenger ikke av GitHub Pages. Får du ikke opp oppsettssiden, åpner du
  `http://127.0.0.1:28417/` direkte i nettleseren eller starter med
  `familiekilder-mcp setup --local`. (Selve innloggingen mot tjenestene krever
  fortsatt nettforbindelse.)

## Oppsett fra terminalen

Slå på tjenestene du trenger. Deaktiverte tjenester registrerer ingen verktøy og
kontaktes ikke.

```sh
familiekilder-mcp configure spond                      # bare lesing
familiekilder-mcp configure hoopit --disable           # slå av igjen
```

Den selvstendige CLI-en og MCP-serveren tilbyr bare lesing. En eldre profil
med skriverettigheter får likevel bare leseverktøy; profilen skrives ikke om.

Innlogging fra terminalen krever at tjenesten har
[klientkonfigurasjon](#klientkonfigurasjon-per-tjeneste). Veiviseren legger inn
det innebygde oppsettet ved første innlogging; terminalen gjør ikke det.
Logg derfor inn første gang i veiviseren, eller importer en fil. Deretter:

```sh
familiekilder-mcp auth vigilo browser-login        # Vigilo, Skolemelding, IST Home, Flyt
familiekilder-mcp auth spond browser-bootstrap     # Spond
familiekilder-mcp auth hoopit token                # Hoopit: lim inn tokenet, det vises ikke
familiekilder-mcp auth spond import-session --session-file /sti/til/.private/spond-økt.json
```

`auth spond browser-bootstrap` fortsetter av seg selv når du er logget inn i
nettleservinduet. Skjer det ikke noe når Spond er ferdig lastet, trykker du Enter
i terminalen. Lukker du vinduet før innloggingen er ferdig, stopper kommandoen
med en melding om det.

Hoopit trenger ingen klientkonfigurasjon. Tokenet lager du selv, som beskrevet
i [Hoopit](hoopit-ingestion.md); det kan også sendes på stdin
(`auth hoopit token --stdin < fil`), men aldri som et argument.

En øktfil til `import-session` må ligge i en mappe som heter `.private` og
bare er tilgjengelig for deg. Innlogging i nettleser åpner en isolert,
midlertidig profil i nettleseren du har valgt; se [nettleseren](#nettleseren). BankID og telefonbekreftelse gjør du
selv. For Vigilo, Skolemelding, IST Home og Flyt ber
kommandoen deg første gang skrive `JEG FORSTÅR` for å bekrefte at integrasjonen
er uoffisiell.

Status og vedlikehold, uten å hente meldinger:

```sh
familiekilder-mcp connections               # alle tjenester: innlogging og rettigheter
familiekilder-mcp auth spond status         # én tjeneste, uten nettverk
familiekilder-mcp auth spond refresh        # forny økten nå
familiekilder-mcp auth spond health         # levetidshistorikk, uten nettverk
familiekilder-mcp auth spond logout
```

Start MCP-serveren (over stdio):

```sh
familiekilder-mcp mcp serve
```

Endringer i konfigurasjonen krever omstart av serveren.

## Koble til agenten din

Agenten starter serveren som en lokal prosess. Oppgi den absolutte stien til
kommandoen og argumentene `mcp serve`:

```sh
codex mcp add familiekilder-mcp -- /ABSOLUTT/STI/familiekilder-mcp/.private/venv/bin/familiekilder-mcp mcp serve
claude mcp add familiekilder-mcp -- /ABSOLUTT/STI/familiekilder-mcp/.private/venv/bin/familiekilder-mcp mcp serve
```

Andre MCP-klienter tar det samme som `command` og `args` i sin konfigurasjon.
Verktøyet `family_connections` viser agenten hvilke tjenester som er slått på,
og hver tjeneste får `<tjeneste>_list_items`, `<tjeneste>_get_item` og
`<tjeneste>_read_attachment`, pluss tjenestens øvrige leseverktøy.

## Klientkonfigurasjon per tjeneste

Se [klientoppsett](klientoppsett.md) for hva hver kobling trenger og hvilke
oppsettsveier som finnes. Bruk gjerne `familiekilder-mcp --version` når du ber
om hjelp.

Vigilo, Skolemelding, Spond og IST Home har innebygd appidentitet, som
veiviseren legger inn når tjenesten mangler oppsett. Appidentiteten er lik for
alle brukere av leverandørens offisielle app og gir ikke tilgang til noen
konto. Flyt spør om kommune.

Har du et eget klientoppsett, importerer du det for én tjeneste om gangen. Et
importert oppsett erstatter det som er lagret, også det innebygde:

```sh
familiekilder-mcp auth <tjeneste> configure --client-file FIL
```

Filen er privat JSON (filmodus 0600, i en privat mappe) og skal aldri ligge i
Git. Feltene kontrolleres av `configure_client` i tjenestens auth-modul
(`vigilo_auth.py`, `skolemelding_auth.py`, `spond_auth.py` og `ist_oauth.py`).
Hoopit har ikke klientoppsett; den bruker et personlig tilgangstoken.
For IST Home kan `familiekilder-mcp ist-auth export-mac-client --output FIL` hente
klientkonfigurasjonen fra IST Home-appen på en Mac.

Den utgitte pakken har ingen betrodde leverandørnøkler og kan derfor ikke
installere en signert providerpakke uten at prosjektets nøkkelliste først
endres. Bruk det innebygde oppsettet eller en egen klientfil.

## Profiler og passord

- `--profile NAVN` gir en egen profil med egne tjenester, økter og passord, uten
  arv fra standardprofilen. `--root MAPPE` velger en annen datarot.
- `familiekilder-mcp password set bankid` lagrer et passord i systemets
  nøkkelring, så innloggingen i nettleseren kan fylle det ut. Det er
  nøkkelringen på macOS, Legitimasjonsbehandling på Windows og Secret Service
  (GNOME Keyring eller KWallet) på Linux. Det finnes ingen filbasert reserve.
  Finnes ingen nøkkelring, er passordhjelperen av, og du skriver passordet i
  nettleservinduet. Innloggingen virker likevel.

Se [profiler og passord](personal-profiles.md).

## Personvern og uoffisielle koblinger

Programmet kjører lokalt. Innlogginger og mellomlagrede meldinger lagres i en
privat mappe med tilgang bare for OS-brukeren din. Det assistenten leser, kan
sendes til AI-leverandøren du har valgt. Assistenten står for oversettelse og
forklaring; denne pakken gir den tilgang til kildene.

Koblingene bruker tjenestenes egne API-er med din innlogging, men er ikke
godkjent av tjenestene. Du må følge tjenestenes vilkår. Endringer hos en
leverandør kan gjøre at en kobling slutter å virke.

## Begrensninger

- Lesing skjer når agenten spør. Det finnes ingen tidsplan, ingen bakgrunnshenting
  og ingen meldingsdatabase. Standardvinduet for lister er 14 dager, og tekstsøk
  filtrerer bare det aktuelle oppslaget.
- Hentede meldinger og vedlegg mellomlagres privat i opptil sju dager. Trafikken
  er begrenset lokalt til 1 000 forespørsler per time og 3 000 per døgn per
  tjeneste; se [mellomlagring](read-cache.md).
- Serveren leser ikke e-post. Bruk assistentens egen Gmail-tilkobling; Claude
  og ChatGPT har hver sin.
- MCP-serveren tilbyr bare leseverktøy. Den sender ikke meldinger, melder
  fravær eller svarer på deltakelse.
- Hvor lenge en innlogging varer, bestemmer tjenesten. Det er ikke kjent for
  alle; se [fornyelse og levetid](auth-lifecycle.md).
- Store PDF-er leses side for side; se [trinnvis PDF-lesing](pdf-window-reading.md).

## Dokumentasjon

| Dokument | Innhold |
|---|---|
| [Prøve på Windows eller Linux](prove-pa-windows-og-linux.md) | Hva du trenger, hva du skal se, forhåndssjekken og hva som er prøvd |
| [Funksjonsmatrise](service-capabilities.md) | Lesing per tjeneste og grenser; intern kode for skriving er merket separat |
| [Fornyelse og levetid](auth-lifecycle.md) | Tokenfornyelse, lagring og hva som er målt |
| [Profiler og passord](personal-profiles.md) | Datarot, profiler og nøkkelring |
| [Mellomlagring](read-cache.md) | Cache, ferskhet og trafikkbudsjett |
| [Hoopit](hoopit-ingestion.md) | Token, kall, versjoner og grenser |
| [Flyt](flyt.md) | Kommunevalg, innlogging, ruter og grenser |
| [Trinnvis PDF-lesing](pdf-window-reading.md) | Vinduer, grenser og binding |
| [Legge til en ny tjeneste](adding-a-service.md) | Filer, kontrakter, registrering og tester |

## Hjelp og bidrag

Meld feil eller spørsmål under **Issues** på prosjektets reposide. Beskriv
kommandoen, feilmeldingen og hvilken tjeneste det gjelder, og lim inn
rapporten fra `familiekilder-mcp doctor`, uten å dele innlogging, tokens, navn
eller innhold fra ekte beskjeder.

### Kjøre testene

```sh
scripts/bootstrap-local-venv
scripts/test                          # alle testene, hver fil i egen prosess
scripts/test tests/test_spond.py      # bare én fil
uvx ruff@0.17.4 check .
```

Testene bruker bare oppdiktede data, falske transporter og midlertidige mapper.
De kontakter ingen ekte tjeneste; én test starter en lokal OIDC-server på
127.0.0.1. Regler for kode og for tilgang til ekte tjenester står i
[AGENTS.md](../AGENTS.md). Vil du koble til en ny tjeneste, start med
[veiledningen](adding-a-service.md).


## Egen vert som prøveordning

Lokal stdio er standard. For HTTP, OAuth, Fly.io og flytting av innlogginger,
se [alltid-på-oppsettet](alltid-pa.md). Innloggingen gjøres fortsatt på
datamaskinen din; ikke kopier øktfiler for hånd.

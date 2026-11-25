# La kodeagenten gjøre installasjonen

Dette passer for en lokalt kjørende kodeagent, for eksempel Codex eller
Claude Code, som har tilgang til filer og terminalen på datamaskinen din. En vanlig
nettchat kan ikke gjøre installasjonen alene.

Last ned kildekoden, åpne prosjektmappen med kodeagenten, og gi den denne
beskjeden:

> Installer familiekilder-mcp fra https://github.com/familiekilder/familiekilder-mcp i ~/familiekilder-mcp. Prosjektet krever Python 3.11+ og Brave, Google Chrome eller Microsoft Edge, på macOS, Windows eller Linux. Kjør scripts/setup.py med Python i bakgrunnen. Det åpner den faste oppsettssiden https://familiekilder.github.io/familiekilder-mcp/oppsett/, som kobler til den lokale veiviseren på http://127.0.0.1:28417. Innlogging og resten av oppsettet gjør jeg selv der.

På Windows må nettleseren være installert for alle brukere, og på Linux fra
leverandørens egen pakke. Windows, Linux og Chrome og Edge på Mac er nytt og
ikke prøvd med ekte innlogging ennå; se
[oppsettsveiledningen](kom-i-gang.md#nettleseren). Den faste oppsettssiden er
prøvd uten synlig vindu, med oppdiktet profil: Brave 156 og Chrome 157 på Mac,
Brave 158 på Ubuntu, og Edge 158 og Chrome 158 på Windows. Den synlige
tillatelsesdialogen, Safari, Firefox, Edge på Mac, og bruk med ekte innlogging
på Windows og Linux er ikke prøvd ennå.

Veiviseren starter som standard på `http://127.0.0.1:28417` og åpner den faste
oppsettssiden `https://familiekilder.github.io/familiekilder-mcp/oppsett/`. På
oppsettssiden klikker du **Sjekk tilkoblingen** (og tillater lokal tilgang hvis
nettleseren spør), og deretter **Fortsett oppsettet**. Siden er statisk på
GitHub Pages; innlogging og lagring skjer bare i den lokale veiviseren. Er port
`28417` opptatt, velges en ledig port og den lokale adressen åpnes direkte.

Veiviseren legger til MCP-koblingen etter ditt valg. Kodeagenten trenger ikke
redigere klientfilene manuelt. Når den lokale MCP-testen er grønn, avslutter du
veiviseren med **Avslutt oppsett**, starter AI-klienten på nytt og velger
familiekilder-mcp i en lokal samtale.

## Kom tilbake til oppsettet

Hvis veiviseren har stoppet mens du var underveis, eller du vil legge til flere
tjenester senere, åpner du `https://familiekilder.github.io/familiekilder-mcp/oppsett/`
eller sender denne beskjeden til kodeagenten:

> Åpne det eksisterende Familiekilder-oppsettet på denne maskinen igjen. Ikke installer på nytt, og ikke oppdater. Kjør i bakgrunnen, og la prosessen stå:
>
> macOS/Linux: `~/familiekilder-mcp/.private/venv/bin/python -I -m familiekilder setup --no-open`
>
> Windows (PowerShell): `& "$HOME\familiekilder-mcp\.private\venv\Scripts\python.exe" -I -m familiekilder setup --no-open`
>
> Brukte vi --root eller --profile sist, sett det samme før setup. Finnes ikke mappen eller miljøet, spør meg. Skriver kommandoen en annen adresse enn http://127.0.0.1:28417, gi meg den adressen. Foreslå gjerne oppdatering, men vent på svar.

Klikk deretter **Prøv igjen** på oppsettssiden, eller åpne adressen agenten gir
deg hvis port `28417` var opptatt. Se
[Kom tilbake til oppsettet](kom-i-gang.md#kom-tilbake-til-oppsettet) for
detaljer om avslått tillatelse, Safari og flere profiler.

Du trenger ingen API-nøkkel til en AI-leverandør for selve oppsettet. Den
AI-klienten du bruker, har sine egne vilkår og eventuelle abonnementskostnader.

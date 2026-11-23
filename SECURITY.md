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

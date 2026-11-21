"""Country normalisation (POLICY principles 5 and 6).

Every country value in the data (ISO-2 / ISO-3 codes, English names and variant spellings,
demonyms such as "Dutch" or "Nigerian", "City, Region, Country" strings, address lines with a
trailing "[Business]" tag, ";"-separated places of birth) is mapped to one canonical English
short name, e.g. "United Kingdom", "Netherlands", "Côte d'Ivoire".

Rules that matter for decisions:
  - Territories are their own country: Puerto Rico != United States, Jersey != United Kingdom,
    Macao != China, Northern Mariana Islands != United States.
  - "International", "Europe" and other regions are not countries -> None.
  - Anything ambiguous ("Congolese", "Korean", "Guinean", "Virgin Islands") -> None, i.e.
    unknown. Unknown can never create a location contradiction, which is the safe direction.
  - Two-letter codes are only read when the whole value is the code (never inside free text).

Pure standard library; no I/O at import time beyond parsing the table below.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# ISO2 | ISO3 | canonical name | aliases (;) | demonyms / adjectives (;)
_TABLE = """
AF|AFG|Afghanistan|Islamic Republic of Afghanistan|Afghan
AX|ALA|Åland Islands|Aland;Aland Islands|Ålander;Alander
AL|ALB|Albania|Republic of Albania|Albanian
DZ|DZA|Algeria|People's Democratic Republic of Algeria|Algerian
AS|ASM|American Samoa||American Samoan
AD|AND|Andorra|Principality of Andorra|Andorran
AO|AGO|Angola|Republic of Angola|Angolan
AI|AIA|Anguilla||Anguillan
AQ|ATA|Antarctica||
AG|ATG|Antigua and Barbuda|Antigua & Barbuda;Antigua|Antiguan;Barbudan
AR|ARG|Argentina|Argentine Republic|Argentine;Argentinian;Argentinean
AM|ARM|Armenia|Republic of Armenia|Armenian
AW|ABW|Aruba||Aruban
AU|AUS|Australia|Commonwealth of Australia|Australian
AT|AUT|Austria|Republic of Austria;Österreich|Austrian
AZ|AZE|Azerbaijan|Republic of Azerbaijan|Azerbaijani;Azeri
BS|BHS|Bahamas|The Bahamas;Commonwealth of the Bahamas|Bahamian
BH|BHR|Bahrain|Kingdom of Bahrain|Bahraini
BD|BGD|Bangladesh|People's Republic of Bangladesh|Bangladeshi
BB|BRB|Barbados||Barbadian;Bajan
BY|BLR|Belarus|Republic of Belarus;Byelorussia|Belarusian;Belarussian;Byelorussian
BE|BEL|Belgium|Kingdom of Belgium;Belgique;België|Belgian
BZ|BLZ|Belize||Belizean
BJ|BEN|Benin|Republic of Benin|Beninese;Beninois
BM|BMU|Bermuda||Bermudian;Bermudan
BT|BTN|Bhutan|Kingdom of Bhutan|Bhutanese
BO|BOL|Bolivia|Plurinational State of Bolivia;Bolivia (Plurinational State of)|Bolivian
BQ|BES|Bonaire, Sint Eustatius and Saba|Caribbean Netherlands;Bonaire|
BA|BIH|Bosnia and Herzegovina|Bosnia & Herzegovina;Bosnia-Herzegovina;Bosnia|Bosnian;Herzegovinian
BW|BWA|Botswana|Republic of Botswana|Motswana;Batswana;Botswanan
BV|BVT|Bouvet Island||
BR|BRA|Brazil|Federative Republic of Brazil;Brasil|Brazilian
IO|IOT|British Indian Ocean Territory||
VG|VGB|British Virgin Islands|Virgin Islands, British;Virgin Islands (British);BVI|British Virgin Islander
BN|BRN|Brunei|Brunei Darussalam|Bruneian
BG|BGR|Bulgaria|Republic of Bulgaria|Bulgarian
BF|BFA|Burkina Faso||Burkinabe;Burkinabè
BI|BDI|Burundi|Republic of Burundi|Burundian
CV|CPV|Cabo Verde|Cape Verde;Republic of Cabo Verde|Cape Verdean;Cabo Verdean
KH|KHM|Cambodia|Kingdom of Cambodia;Kampuchea|Cambodian;Khmer
CM|CMR|Cameroon|Republic of Cameroon|Cameroonian
CA|CAN|Canada||Canadian
KY|CYM|Cayman Islands|Caymans|Caymanian
CF|CAF|Central African Republic|CAR|Central African
TD|TCD|Chad|Republic of Chad|Chadian
CL|CHL|Chile|Republic of Chile|Chilean
CN|CHN|China|People's Republic of China;PRC;Mainland China|Chinese
CX|CXR|Christmas Island||Christmas Islander
CC|CCK|Cocos (Keeling) Islands|Cocos Islands;Keeling Islands|
CO|COL|Colombia|Republic of Colombia|Colombian
KM|COM|Comoros|Union of the Comoros|Comoran;Comorian
CG|COG|Republic of the Congo|Congo;Congo-Brazzaville;Congo (Brazzaville);Republic of Congo|
CD|COD|Democratic Republic of the Congo|DR Congo;DRC;Congo-Kinshasa;Congo (Kinshasa);Congo, Democratic Republic of the;Democratic Republic of Congo;Zaire|
CK|COK|Cook Islands||Cook Islander
CR|CRI|Costa Rica|Republic of Costa Rica|Costa Rican
CI|CIV|Côte d'Ivoire|Cote d'Ivoire;Cote D'Ivoire;Ivory Coast;Republic of Côte d'Ivoire|Ivorian
HR|HRV|Croatia|Republic of Croatia;Hrvatska|Croatian;Croat
CU|CUB|Cuba|Republic of Cuba|Cuban
CW|CUW|Curaçao|Curacao|Curaçaoan;Curacaoan
CY|CYP|Cyprus|Republic of Cyprus|Cypriot
CZ|CZE|Czech Republic|Czechia;Czech Rep.|Czech
DK|DNK|Denmark|Kingdom of Denmark;Danmark|Danish
DJ|DJI|Djibouti|Republic of Djibouti|Djiboutian
DM|DMA|Dominica|Commonwealth of Dominica|
DO|DOM|Dominican Republic|Dominican Rep.|Dominican
EC|ECU|Ecuador|Republic of Ecuador|Ecuadorian;Ecuadorean
EG|EGY|Egypt|Arab Republic of Egypt|Egyptian
SV|SLV|El Salvador|Republic of El Salvador|Salvadoran;Salvadorean
GQ|GNQ|Equatorial Guinea|Republic of Equatorial Guinea|Equatoguinean;Equatorial Guinean
ER|ERI|Eritrea|State of Eritrea|Eritrean
EE|EST|Estonia|Republic of Estonia;Eesti|Estonian
SZ|SWZ|Eswatini|Swaziland;Kingdom of Eswatini|Swazi;Swati
ET|ETH|Ethiopia|Federal Democratic Republic of Ethiopia|Ethiopian
FK|FLK|Falkland Islands|Falkland Islands (Malvinas);Malvinas;Falklands|Falkland Islander
FO|FRO|Faroe Islands|Faeroe Islands;Faroes|Faroese
FJ|FJI|Fiji|Republic of Fiji|Fijian
FI|FIN|Finland|Republic of Finland;Suomi|Finnish
FR|FRA|France|French Republic|French
GF|GUF|French Guiana|Guyane|French Guianese
PF|PYF|French Polynesia|Tahiti|French Polynesian;Tahitian
TF|ATF|French Southern Territories|French Southern and Antarctic Lands|
GA|GAB|Gabon|Gabonese Republic|Gabonese
GM|GMB|Gambia|The Gambia;Republic of the Gambia|Gambian
GE|GEO|Georgia|Sakartvelo|Georgian
DE|DEU|Germany|Federal Republic of Germany;Deutschland|German
GH|GHA|Ghana|Republic of Ghana|Ghanaian
GI|GIB|Gibraltar||Gibraltarian
GR|GRC|Greece|Hellenic Republic;Hellas|Greek;Hellenic
GL|GRL|Greenland|Kalaallit Nunaat|Greenlandic;Greenlander
GD|GRD|Grenada||Grenadian
GP|GLP|Guadeloupe||Guadeloupean
GU|GUM|Guam||Guamanian;Chamorro
GT|GTM|Guatemala|Republic of Guatemala|Guatemalan
GG|GGY|Guernsey|Bailiwick of Guernsey|Guernseyman
GN|GIN|Guinea|Republic of Guinea;Guinea-Conakry|
GW|GNB|Guinea-Bissau|Guinea Bissau;Republic of Guinea-Bissau|Bissau-Guinean
GY|GUY|Guyana|Co-operative Republic of Guyana|Guyanese
HT|HTI|Haiti|Republic of Haiti|Haitian
HM|HMD|Heard Island and McDonald Islands||
VA|VAT|Holy See|Vatican City;Vatican;Vatican City State|Vatican
HN|HND|Honduras|Republic of Honduras|Honduran
HK|HKG|Hong Kong|Hong Kong SAR;Hong Kong Special Administrative Region of China;Hong Kong, China;Hong Kong Special Administrative Region|Hongkonger;Hong Konger
HU|HUN|Hungary|Magyarország|Hungarian;Magyar
IS|ISL|Iceland|Ísland|Icelandic;Icelander
IN|IND|India|Republic of India|Indian
ID|IDN|Indonesia|Republic of Indonesia|Indonesian
IR|IRN|Iran|Islamic Republic of Iran;Iran (Islamic Republic of);Iran, Islamic Republic of;Persia|Iranian
IQ|IRQ|Iraq|Republic of Iraq|Iraqi
IE|IRL|Ireland|Republic of Ireland;Eire;Éire|Irish
IM|IMN|Isle of Man||Manx
IL|ISR|Israel|State of Israel|Israeli
IT|ITA|Italy|Italian Republic;Italia|Italian
JM|JAM|Jamaica||Jamaican
JP|JPN|Japan|Nippon|Japanese
JE|JEY|Jersey|Bailiwick of Jersey|Jèrriais
JO|JOR|Jordan|Hashemite Kingdom of Jordan|Jordanian
KZ|KAZ|Kazakhstan|Republic of Kazakhstan|Kazakh;Kazakhstani
KE|KEN|Kenya|Republic of Kenya|Kenyan
KI|KIR|Kiribati||I-Kiribati
KP|PRK|North Korea|Democratic People's Republic of Korea;DPRK;Korea, Democratic People's Republic of;Korea (North);Korea, North|North Korean
KR|KOR|South Korea|Republic of Korea;Korea, Republic of;Korea (South);Korea, South|South Korean
XK|XKX|Kosovo|Republic of Kosovo;Kosova|Kosovar;Kosovan
KW|KWT|Kuwait|State of Kuwait|Kuwaiti
KG|KGZ|Kyrgyzstan|Kyrgyz Republic;Kirghizia|Kyrgyz;Kyrgyzstani;Kirghiz
LA|LAO|Laos|Lao People's Democratic Republic;Lao PDR;Lao|Laotian
LV|LVA|Latvia|Republic of Latvia;Latvija|Latvian;Lettish
LB|LBN|Lebanon|Lebanese Republic|Lebanese
LS|LSO|Lesotho|Kingdom of Lesotho|Basotho;Mosotho
LR|LBR|Liberia|Republic of Liberia|Liberian
LY|LBY|Libya|State of Libya;Libyan Arab Jamahiriya|Libyan
LI|LIE|Liechtenstein|Principality of Liechtenstein|Liechtensteiner;Liechtensteinian
LT|LTU|Lithuania|Republic of Lithuania;Lietuva|Lithuanian
LU|LUX|Luxembourg|Grand Duchy of Luxembourg;Luxemburg|Luxembourger;Luxembourgish
MO|MAC|Macao|Macau;Macao SAR;Macao Special Administrative Region of China;Macau Special Administrative Region of China;Macao, China;Macao Special Administrative Region|Macanese
MG|MDG|Madagascar|Republic of Madagascar|Malagasy
MW|MWI|Malawi|Republic of Malawi|Malawian
MY|MYS|Malaysia||Malaysian
MV|MDV|Maldives|Republic of Maldives|Maldivian
ML|MLI|Mali|Republic of Mali|Malian
MT|MLT|Malta|Republic of Malta|Maltese
MH|MHL|Marshall Islands|Republic of the Marshall Islands|Marshallese
MQ|MTQ|Martinique||Martinican;Martiniquais
MR|MRT|Mauritania|Islamic Republic of Mauritania|Mauritanian
MU|MUS|Mauritius|Republic of Mauritius|Mauritian
YT|MYT|Mayotte||Mahoran
MX|MEX|Mexico|United Mexican States;México|Mexican
FM|FSM|Micronesia|Federated States of Micronesia;Micronesia (Federated States of)|Micronesian
MD|MDA|Moldova|Republic of Moldova;Moldova, Republic of|Moldovan
MC|MCO|Monaco|Principality of Monaco|Monegasque;Monacan
MN|MNG|Mongolia||Mongolian
ME|MNE|Montenegro|Crna Gora|Montenegrin
MS|MSR|Montserrat||Montserratian
MA|MAR|Morocco|Kingdom of Morocco|Moroccan
MZ|MOZ|Mozambique|Republic of Mozambique|Mozambican
MM|MMR|Myanmar|Burma;Republic of the Union of Myanmar|Burmese;Myanmarese
NA|NAM|Namibia|Republic of Namibia|Namibian
NR|NRU|Nauru|Republic of Nauru|Nauruan
NP|NPL|Nepal|Federal Democratic Republic of Nepal|Nepali;Nepalese
NL|NLD|Netherlands|The Netherlands;Holland;Kingdom of the Netherlands;Nederland|Dutch;Netherlander
NC|NCL|New Caledonia||New Caledonian
NZ|NZL|New Zealand|Aotearoa|New Zealander
NI|NIC|Nicaragua|Republic of Nicaragua|Nicaraguan
NE|NER|Niger|Republic of the Niger|Nigerien
NG|NGA|Nigeria|Federal Republic of Nigeria|Nigerian
NU|NIU|Niue||Niuean
NF|NFK|Norfolk Island||Norfolk Islander
MK|MKD|North Macedonia|Macedonia;Republic of North Macedonia;FYROM;Former Yugoslav Republic of Macedonia|Macedonian;North Macedonian
MP|MNP|Northern Mariana Islands|Commonwealth of the Northern Mariana Islands;Northern Marianas;CNMI|Northern Mariana Islander
NO|NOR|Norway|Kingdom of Norway;Norge|Norwegian
OM|OMN|Oman|Sultanate of Oman|Omani
PK|PAK|Pakistan|Islamic Republic of Pakistan|Pakistani
PW|PLW|Palau|Republic of Palau|Palauan
PS|PSE|Palestine|Palestina;State of Palestine;Palestinian Territories;Occupied Palestinian Territory;Palestine, State of|Palestinian
PA|PAN|Panama|Republic of Panama;Panamá|Panamanian
PG|PNG|Papua New Guinea|Independent State of Papua New Guinea|Papua New Guinean
PY|PRY|Paraguay|Republic of Paraguay|Paraguayan
PE|PER|Peru|Republic of Peru;Perú|Peruvian
PH|PHL|Philippines|Republic of the Philippines;The Philippines|Filipino;Filipina;Philippine
PN|PCN|Pitcairn|Pitcairn Islands|Pitcairn Islander
PL|POL|Poland|Republic of Poland;Polska|Polish
PT|PRT|Portugal|Portuguese Republic|Portuguese
PR|PRI|Puerto Rico|Commonwealth of Puerto Rico|Puerto Rican
QA|QAT|Qatar|State of Qatar|Qatari
RE|REU|Réunion|Reunion|Réunionese;Reunionese
RO|ROU|Romania|Rumania;Roumania|Romanian
RU|RUS|Russia|Russian Federation|Russian
RW|RWA|Rwanda|Republic of Rwanda|Rwandan
BL|BLM|Saint Barthélemy|Saint Barthelemy;St Barthelemy;St. Barts;St Barts|Barthélemois
SH|SHN|Saint Helena|Saint Helena, Ascension and Tristan da Cunha;St Helena|Saint Helenian
KN|KNA|Saint Kitts and Nevis|St Kitts and Nevis;St. Kitts and Nevis;Saint Kitts & Nevis|Kittitian;Nevisian
LC|LCA|Saint Lucia|St Lucia;St. Lucia|Saint Lucian
MF|MAF|Saint Martin|Saint Martin (French part);St Martin|Saint-Martinoise
PM|SPM|Saint Pierre and Miquelon|St Pierre and Miquelon|
VC|VCT|Saint Vincent and the Grenadines|St Vincent and the Grenadines;St. Vincent and the Grenadines;Saint Vincent|Vincentian
WS|WSM|Samoa|Independent State of Samoa|Samoan
SM|SMR|San Marino|Republic of San Marino|Sammarinese
ST|STP|Sao Tome and Principe|São Tomé and Príncipe|Santomean
SA|SAU|Saudi Arabia|Kingdom of Saudi Arabia;KSA|Saudi;Saudi Arabian
SN|SEN|Senegal|Republic of Senegal|Senegalese
RS|SRB|Serbia|Republic of Serbia;Srbija|Serbian;Serb
SC|SYC|Seychelles|Republic of Seychelles|Seychellois
SL|SLE|Sierra Leone|Republic of Sierra Leone|Sierra Leonean
SG|SGP|Singapore|Republic of Singapore|Singaporean
SX|SXM|Sint Maarten|Sint Maarten (Dutch part)|Sint Maartener
SK|SVK|Slovakia|Slovak Republic|Slovak;Slovakian
SI|SVN|Slovenia|Republic of Slovenia;Slovenija|Slovenian;Slovene
SB|SLB|Solomon Islands||Solomon Islander
SO|SOM|Somalia|Federal Republic of Somalia|Somali;Somalian
ZA|ZAF|South Africa|Republic of South Africa;RSA|South African
GS|SGS|South Georgia and the South Sandwich Islands|South Georgia|
SS|SSD|South Sudan|Republic of South Sudan|South Sudanese
ES|ESP|Spain|Kingdom of Spain;España;Espana|Spanish;Spaniard
LK|LKA|Sri Lanka|Democratic Socialist Republic of Sri Lanka;Ceylon|Sri Lankan
SD|SDN|Sudan|Republic of the Sudan;The Sudan|Sudanese
SR|SUR|Suriname|Surinam;Republic of Suriname|Surinamese;Surinamer
SJ|SJM|Svalbard and Jan Mayen|Svalbard|
SE|SWE|Sweden|Kingdom of Sweden;Sverige|Swedish
CH|CHE|Switzerland|Swiss Confederation;Schweiz;Suisse;Svizzera|Swiss
SY|SYR|Syria|Syrian Arab Republic|Syrian
TW|TWN|Taiwan|Republic of China;Taiwan, Province of China;Chinese Taipei|Taiwanese
TJ|TJK|Tajikistan|Republic of Tajikistan|Tajik;Tajikistani
TZ|TZA|Tanzania|United Republic of Tanzania;Tanzania, United Republic of|Tanzanian
TH|THA|Thailand|Kingdom of Thailand;Siam|Thai
TL|TLS|Timor-Leste|East Timor;Timor Leste|Timorese;East Timorese
TG|TGO|Togo|Togolese Republic|Togolese
TK|TKL|Tokelau||Tokelauan
TO|TON|Tonga|Kingdom of Tonga|Tongan
TT|TTO|Trinidad and Tobago|Trinidad & Tobago;Trinidad|Trinidadian;Tobagonian
TN|TUN|Tunisia|Republic of Tunisia|Tunisian
TR|TUR|Turkey|Türkiye;Turkiye;Republic of Turkey|Turkish;Turk
TM|TKM|Turkmenistan||Turkmen;Turkmenistani
TC|TCA|Turks and Caicos Islands|Turks & Caicos Islands;Turks and Caicos|Turks and Caicos Islander
TV|TUV|Tuvalu||Tuvaluan
UG|UGA|Uganda|Republic of Uganda|Ugandan
UA|UKR|Ukraine||Ukrainian
AE|ARE|United Arab Emirates|UAE;U.A.E.;Emirates|Emirati;Emirian
GB|GBR|United Kingdom|UK;U.K.;Great Britain;Britain;United Kingdom of Great Britain and Northern Ireland;England;Scotland;Wales;Northern Ireland|British;English;Scottish;Scots;Welsh;Northern Irish;Briton
UM|UMI|United States Minor Outlying Islands|US Minor Outlying Islands|
US|USA|United States|United States of America;USA;U.S.A.;U.S.;America;US of A|American;US citizen
UY|URY|Uruguay|Oriental Republic of Uruguay|Uruguayan
UZ|UZB|Uzbekistan|Republic of Uzbekistan|Uzbek;Uzbekistani
VU|VUT|Vanuatu|Republic of Vanuatu|Ni-Vanuatu;Vanuatuan
VE|VEN|Venezuela|Bolivarian Republic of Venezuela;Venezuela (Bolivarian Republic of)|Venezuelan
VN|VNM|Vietnam|Viet Nam;Socialist Republic of Vietnam|Vietnamese
VI|VIR|United States Virgin Islands|US Virgin Islands;U.S. Virgin Islands;Virgin Islands, U.S.;Virgin Islands (U.S.);USVI|
WF|WLF|Wallis and Futuna|Wallis & Futuna|Wallisian;Futunan
EH|ESH|Western Sahara|Sahrawi Arab Democratic Republic|Sahrawi;Sahraoui
YE|YEM|Yemen|Republic of Yemen|Yemeni
ZM|ZMB|Zambia|Republic of Zambia|Zambian
ZW|ZWE|Zimbabwe|Republic of Zimbabwe|Zimbabwean
"""

# Values that look like places but are not one country. They normalise to None, and in free
# text they shadow shorter country words they contain ("New Mexico" is not Mexico).
_NOT_COUNTRIES = (
    "international", "worldwide", "global", "europe", "european", "european union", "eu",
    "africa", "african", "asia", "asian", "middle east", "latin america", "latin american",
    "south america", "south american", "north america", "north american", "central america",
    "central american", "caribbean", "oceania", "scandinavia", "scandinavian", "balkans",
    "gulf", "arab", "arabian", "hispanic", "latino",
    "unknown", "n/a", "na", "none", "null", "other", "stateless", "various", "multiple",
    # ambiguous between several countries -> unknown
    "congolese", "korean", "korea", "guinean", "virgin islands", "virgin islander", "samoa islands",
    "congo", "the congo",
    # US states / regions that contain a country word
    "new mexico", "new jersey", "new south wales", "new england", "papua",
    "georgia usa", "georgia (us state)", "west virginia", "virginia",
)

_ALIASES: dict[str, str] = {}
_CODE2: dict[str, str] = {}
_CODE3: dict[str, str] = {}
_ISO2_OF: dict[str, str] = {}
_DEMONYM_KEYS: set[str] = set()


def _fold(text: str) -> str:
    """Lowercase, strip accents, unify apostrophes and whitespace."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'").replace("‘", "'")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[\s_]+", " ", text)
    return text.strip(" .,;:-")


def _key(text: str) -> str:
    """Lookup key: folded, punctuation other than apostrophes/parentheses removed."""
    text = _fold(text)
    text = re.sub(r"[.\-/]", " ", text)
    text = re.sub(r"[^\w' ()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _register(name: str, canonical: str | None, demonym: bool = False) -> None:
    key = _key(name)
    if not key:
        return
    if key in _ALIASES and _ALIASES[key] != canonical:
        _ALIASES[key] = None  # collision -> ambiguous -> unknown
        return
    _ALIASES[key] = canonical
    if demonym:
        _DEMONYM_KEYS.add(key)


def _build() -> None:
    for line in _TABLE.strip().splitlines():
        iso2, iso3, name, aliases, demonyms = (part.strip() for part in line.split("|"))
        _CODE2[iso2] = name
        _CODE3[iso3] = name
        _ISO2_OF[name] = iso2
        _register(name, name)
        for alias in filter(None, (a.strip() for a in aliases.split(";"))):
            _register(alias, name)
        for dem in filter(None, (d.strip() for d in demonyms.split(";"))):
            _register(dem, name, demonym=True)
    for word in _NOT_COUNTRIES:
        _ALIASES[_key(word)] = None


_build()

ALL_COUNTRIES: tuple[str, ...] = tuple(sorted(_ISO2_OF))
_TAG = re.compile(r"\s*\[[^\]]*\]\s*$")


def _lookup(value: str) -> str | None:
    """Exact lookup of one cleaned value (code, name, alias or demonym)."""
    raw = value.strip()
    if not raw or raw.upper() in ("NA", "N/A"):  # NA is far more often "not available" than Namibia
        return None
    if re.fullmatch(r"[A-Za-z]{2}", raw) and raw.upper() in _CODE2 and raw.isupper():
        return _CODE2[raw.upper()]
    if re.fullmatch(r"[A-Z]{3}", raw) and raw in _CODE3:
        return _CODE3[raw]
    key = _key(raw)
    if key in _ALIASES:
        return _ALIASES[key]
    # "The Gambia", "the Netherlands"
    if key.startswith("the ") and key[4:] in _ALIASES:
        return _ALIASES[key[4:]]
    # "Mexican national", "Nigerian citizen"
    stripped = re.sub(r"\s+(national|citizen|nationality|resident|passport)$", "", key)
    if stripped != key and stripped in _ALIASES:
        return _ALIASES[stripped]
    return None


def normalize_country(value: Any) -> str | None:
    """Canonical country name for one whole value, or None if unknown / not a country.

    Accepts ISO-2 (upper case only, so the English word "in" is never India), ISO-3, names,
    aliases, demonyms, and a trailing "[Tag]". Does not split "City, Country" strings: use
    country_from_location for those.
    """
    if value is None:
        return None
    text = _TAG.sub("", str(value)).strip()
    return _lookup(text) if text else None


def country_from_location(value: Any) -> str | None:
    """Country of a "City, Region, Country" style value (address, nationality, place of birth).

    The whole value is tried first, then the last comma part, then the other parts from right
    to left (so "Belgrade, Serbia" -> Serbia and "Fayette County, Atlanta, Georgia,
    United States" -> United States, not Georgia).
    """
    if value is None:
        return None
    text = _TAG.sub("", str(value)).strip()
    if not text:
        return None
    whole = _lookup(text)
    if whole is not None or _key(text) in _ALIASES:
        return whole
    parts = [p.strip() for p in text.split(",") if p.strip()]
    for part in reversed(parts):
        part = _TAG.sub("", part)
        found = _lookup(part)
        if found is not None:
            return found
        if _key(part) in _ALIASES:  # an explicit non-country such as "Europe": stop here
            return None
    return None


def place_of_birth_countries(value: Any) -> tuple[str, ...]:
    """Countries of a place_of_birth value; ";" separates alternative places."""
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple)) else str(value).split(";")
    found = {country_from_location(v) for v in values}
    return tuple(sorted(c for c in found if c))


def iso2(country: str | None) -> str | None:
    """ISO-2 code of a canonical name (or of anything normalize_country understands)."""
    if country is None:
        return None
    name = country if country in _ISO2_OF else normalize_country(country)
    return _ISO2_OF.get(name) if name else None


def same_country(a: Any, b: Any) -> bool:
    """True when both values are known and name the same country."""
    ca, cb = normalize_country(a), normalize_country(b)
    return ca is not None and ca == cb


def countries_overlap(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    """Canonical countries present on both sides (sorted). Inputs must already be canonical."""
    return tuple(sorted(set(filter(None, left)) & set(filter(None, right))))


# ----------------------------------------------------------------------------- free text

# Whole-value aliases that are too common as ordinary words or surnames to read from free text.
_TEXT_EXCLUDE = {"holland", "america", "britain", "emirates", "trinidad", "antigua", "guyane", "lao"}
_TEXT_KEYS = sorted((k for k in _ALIASES if k not in _TEXT_EXCLUDE), key=lambda k: (-len(k), k))
_TEXT_PATTERN = re.compile(
    r"(?<![\w'])(" + "|".join(re.escape(k) for k in _TEXT_KEYS if len(k) >= 4 or k in ("uk", "usa", "uae")) + r")(?![\w])"
)


def country_mentions(text: Any) -> tuple[str, ...]:
    """Countries named in free text by name, alias or demonym (sorted, distinct).

    Longest phrases win, so "New Mexico", "South African" and "Papua New Guinea" are read as
    themselves. Two-letter codes are never read from free text. Region words return nothing.
    """
    if not text:
        return ()
    folded = _key(str(text))
    found = set()
    for match in _TEXT_PATTERN.finditer(folded):
        country = _ALIASES.get(match.group(1))
        if country:
            found.add(country)
    return tuple(sorted(found))


def hit_country_sources(hit: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """(country, field, raw value) for every country tied to a watchlist hit (POLICY 5).

    Fields: countries, nationality, citizenship, addresses (last part of each) and
    place_of_birth. Regions such as "International" or "Europe" are skipped.
    """
    out = []
    for field in ("countries", "nationality", "citizenship"):
        for raw in hit.get(field) or []:
            country = country_from_location(raw)
            if country:
                out.append((country, field, str(raw)))
    for raw in hit.get("addresses") or []:
        if isinstance(raw, dict):
            raw = raw.get("country") or ", ".join(str(v) for v in raw.values() if v)
        country = country_from_location(raw)
        if country:
            out.append((country, "addresses", str(raw)))
    for country in place_of_birth_countries(hit.get("place_of_birth")):
        out.append((country, "place_of_birth", str(hit.get("place_of_birth"))))
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return tuple(unique)


def hit_countries(hit: dict[str, Any]) -> tuple[str, ...]:
    """Distinct canonical countries tied to a hit, sorted."""
    return tuple(sorted({c for c, _, _ in hit_country_sources(hit)}))

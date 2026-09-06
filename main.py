from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import anthropic
import os
import json
import traceback
import time
import uuid
import resend
from collections import defaultdict
from datetime import datetime
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()

class PlatPartage(Base):
    __tablename__ = "plats_partages"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nom = Column(String, nullable=False, unique=True)
    calories = Column(Integer, default=0)
    proteines_g = Column(Integer, default=0)
    glucides_g = Column(Integer, default=0)
    lipides_g = Column(Integer, default=0)
    score = Column(Integer, default=0)
    verdict = Column(String, default="")
    commentaire = Column(Text, default="")
    nutrients = Column(Text, default="[]")
    conseils = Column(Text, default="[]")
    valide = Column(Boolean, default=True)
    date_creation = Column(DateTime, default=datetime.utcnow)
    nombre_utilisations = Column(Integer, default=1)

class CorrectionPending(Base):
    __tablename__ = "corrections_pending"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    plat_id = Column(String, nullable=True)
    nom_original = Column(String, nullable=False)
    nom_corrige = Column(String, nullable=False)
    calories_corrige = Column(Integer, default=0)
    proteines_corrige = Column(Integer, default=0)
    glucides_corrige = Column(Integer, default=0)
    lipides_corrige = Column(Integer, default=0)
    user_id = Column(String, nullable=False)
    statut = Column(String, default="pending")
    date_soumission = Column(DateTime, default=datetime.utcnow)

if engine:
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

# MARK: — Email (Resend)
resend.api_key = os.environ.get("RESEND_API_KEY", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

def envoyer_email_correction(correction_id: str, nom_original: str, nom_corrige: str, user_id: str):
    if not resend.api_key or not ADMIN_EMAIL:
        print("⚠️ Email non configuré")
        return
    
    lien_valider = f"https://web-production-c1f45.up.railway.app/admin/valider/{correction_id}"
    lien_rejeter = f"https://web-production-c1f45.up.railway.app/admin/rejeter/{correction_id}"
    
    try:
        resend.Emails.send({
            "from": "nutriscan@resend.dev",
            "to": ADMIN_EMAIL,
            "subject": f"NutriScan — Correction à valider : {nom_original}",
            "html": f"""
            <h2>Nouvelle correction soumise</h2>
            <p><strong>Utilisateur :</strong> {user_id[:8]}...</p>
            <p><strong>Nom original :</strong> {nom_original}</p>
            <p><strong>Nom corrigé :</strong> {nom_corrige}</p>
            <br>
            <a href="{lien_valider}" style="background:#22c55e;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;margin-right:12px">
                ✅ Valider
            </a>
            <a href="{lien_rejeter}" style="background:#ef4444;color:white;padding:12px 24px;border-radius:6px;text-decoration:none">
                ❌ Rejeter
            </a>
            """
        })
        print(f"📧 Email envoyé pour correction {correction_id}")
    except Exception as e:
        print(f"❌ Erreur email: {e}")


# MARK: — Quotas
MAX_ANALYSES_PAR_JOUR = 100
user_analyses = defaultdict(list)

def verifier_quota(user_id: str) -> dict:
    now = time.time()
    hier = now - 86400
    user_analyses[user_id] = [t for t in user_analyses[user_id] if t > hier]
    appels = len(user_analyses[user_id])
    return {
        "autorise": appels < MAX_ANALYSES_PAR_JOUR,
        "appels_aujourd_hui": appels,
        "restants": max(0, MAX_ANALYSES_PAR_JOUR - appels),
        "maximum": MAX_ANALYSES_PAR_JOUR
    }

def enregistrer_appel(user_id: str):
    user_analyses[user_id].append(time.time())


# MARK: — Modèles Pydantic
class AnalyzeRequest(BaseModel):
    image_base64: str
    age: int
    gender: str
    weight: int
    goal: str
    poids_plat: int
    nom_plat: str | None = None
    user_id: str = "anonymous"

class SuggestionsRequest(BaseModel):
    prompt: str

class NextMealRequest(BaseModel):
    nom_repas: str
    score: int
    nutrients: list
    aliments_frigo: list[str] = []

class ScanInventoryRequest(BaseModel):
    image_base64: str

class RecipeRequest(BaseModel):
    aliments: list[str]
    aliment_principal: str | None = None

class CorrectionRequest(BaseModel):
    nom_original: str
    nom_corrige: str
    calories: int
    proteines_g: int
    glucides_g: int
    lipides_g: int
    user_id: str

class ScanMenuRequest(BaseModel):
    image_base64: str
    semaine: str

class AnalysePlatCantineRequest(BaseModel):
    nom_plat: str
    type_plat: str

class ScanReceiptRequest(BaseModel):
    prompt: str


# MARK: — Normalisation numérique
def _to_int(valeur, defaut=0):
    """Convertit n'importe quoi en int : 72.5 -> 72, "650" -> 650, None -> defaut."""
    try:
        if valeur is None:
            return defaut
        if isinstance(valeur, bool):
            return defaut
        if isinstance(valeur, (int, float)):
            return int(round(float(valeur)))
        texte = str(valeur).strip().replace(",", ".")
        texte = re.sub(r"[^0-9.\-]", "", texte)
        return int(round(float(texte))) if texte not in ("", "-", ".") else defaut
    except Exception:
        return defaut


def normaliser_resultat(result: dict) -> dict:
    """Garantit que tous les champs numériques sont des entiers (Swift attend des Int)."""
    if not isinstance(result, dict):
        return result

    result["score"] = _to_int(result.get("score"))
    result["nom"] = str(result.get("nom", "")).strip()
    result["description"] = str(result.get("description", "")).strip()
    result["verdict"] = str(result.get("verdict", "")).strip()
    result["commentaire"] = str(result.get("commentaire", "")).strip()

    macros = result.get("macros") or {}
    result["macros"] = {
        "calories": _to_int(macros.get("calories")),
        "proteines_g": _to_int(macros.get("proteines_g")),
        "glucides_g": _to_int(macros.get("glucides_g")),
        "lipides_g": _to_int(macros.get("lipides_g")),
    }

    nutrients = result.get("nutrients") or []
    result["nutrients"] = [
        {
            "nom": str(n.get("nom", "")).strip(),
            "pct": _to_int(n.get("pct")),
            "niveau": str(n.get("niveau", "medium")).strip(),
        }
        for n in nutrients
        if isinstance(n, dict)
    ]

    conseils = result.get("conseils") or []
    result["conseils"] = [str(c).strip() for c in conseils if str(c).strip()]

    # Décomposition en ingrédients : mêmes contraintes de type que le reste,
    # et on écarte les lignes sans nom ou sans masse, inexploitables côté app.
    ingredients = result.get("ingredients") or []
    result["ingredients"] = [
        {
            "nom": str(i.get("nom", "")).strip(),
            "grammes": _to_int(i.get("grammes")),
            "calories": _to_int(i.get("calories")),
            "proteines_g": _to_int(i.get("proteines_g")),
            "glucides_g": _to_int(i.get("glucides_g")),
            "lipides_g": _to_int(i.get("lipides_g")),
        }
        for i in ingredients
        if isinstance(i, dict) and str(i.get("nom", "")).strip()
        and _to_int(i.get("grammes")) > 0
    ]

    return result


# MARK: — Helpers base partagée
def chercher_plat_partage(nom: str):
    if not engine:
        return None
    session = Session()
    try:
        nom_lower = nom.lower().strip()
        plats = session.query(PlatPartage).filter(PlatPartage.valide == True).all()
        
        def mots_significatifs(texte):
            mots = texte.lower().strip().split()
            return set(m[:-1] if m.endswith("s") and len(m) > 3 else m for m in mots if len(m) > 2)
        
        mots_recherche = mots_significatifs(nom_lower)
        
        for plat in plats:
            mots_plat = mots_significatifs(plat.nom)
            intersection = mots_plat.intersection(mots_recherche)
            union = min(len(mots_plat), len(mots_recherche))
            if union > 0 and len(intersection) / union >= 0.6:
                plat.nombre_utilisations += 1
                session.commit()
                print(f"✅ Trouvé dans base partagée : '{plat.nom}' pour '{nom}'")
                return plat
        return None
    except Exception as e:
        print(f"Erreur recherche plat: {e}")
        return None
    finally:
        session.close()

def sauvegarder_plat_partage(result: dict):
    if not engine:
        return
    session = Session()
    try:
        nom = result.get("nom", "")
        plats = session.query(PlatPartage).filter(PlatPartage.valide == True).all()
        
        def mots(texte):
            m = texte.lower().strip().split()
            return set(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in m if len(w) > 2)
        
        mots_nom = mots(nom)
        plat_existant = None
        for p in plats:
            inter = mots(p.nom).intersection(mots_nom)
            uni = min(len(mots(p.nom)), len(mots_nom))
            if uni > 0 and len(inter) / uni >= 0.6:
                plat_existant = p
                break
        
        if plat_existant:
            macros = result.get("macros", {})
            plat_existant.calories = macros.get("calories", plat_existant.calories)
            plat_existant.proteines_g = macros.get("proteines_g", plat_existant.proteines_g)
            plat_existant.glucides_g = macros.get("glucides_g", plat_existant.glucides_g)
            plat_existant.lipides_g = macros.get("lipides_g", plat_existant.lipides_g)
            plat_existant.score = result.get("score", plat_existant.score)
            plat_existant.nombre_utilisations += 1
            session.commit()
            print(f"🔄 Plat mis à jour dans base partagée : {nom}")
        else:
            macros = result.get("macros", {})
            nouveau = PlatPartage(
                id=str(uuid.uuid4()),
                nom=nom,
                calories=macros.get("calories", 0),
                proteines_g=macros.get("proteines_g", 0),
                glucides_g=macros.get("glucides_g", 0),
                lipides_g=macros.get("lipides_g", 0),
                score=result.get("score", 0),
                verdict=result.get("verdict", ""),
                commentaire=result.get("commentaire", ""),
                nutrients=json.dumps(result.get("nutrients", [])),
                conseils=json.dumps(result.get("conseils", [])),
                valide=True
            )
            session.add(nouveau)
            session.commit()
            print(f"💾 Nouveau plat dans base partagée : {nom}")
    except Exception as e:
        print(f"Erreur sauvegarde plat: {e}")
        session.rollback()
    finally:
        session.close()


# MARK: — Parsing robuste des réponses Claude
def parser_json_claude(response, defaut=None, contexte=""):
    """Extrait un JSON d'une réponse Claude, même tronquée ou entourée de markdown.

    Renvoie `defaut` (ou {}) si rien d'exploitable n'est trouvé.
    """
    defaut = {} if defaut is None else defaut

    try:
        raw = response.content[0].text
    except Exception:
        print(f"[{contexte}] ❌ Réponse vide")
        return defaut

    tronque = getattr(response, "stop_reason", None) == "max_tokens"
    if tronque:
        print(f"[{contexte}] ⚠️ Réponse tronquée par max_tokens ({len(raw)} chars)")

    clean = raw.replace("```json", "").replace("```", "").strip()

    # Guillemets typographiques éventuels
    clean = clean.replace("\u201c", '"').replace("\u201d", '"')

    # Objet {...} ou tableau [...] : on prend ce qui commence en premier
    debut_obj = clean.find("{")
    debut_arr = clean.find("[")
    if debut_obj == -1 and debut_arr == -1:
        print(f"[{contexte}] ❌ Aucun JSON détecté")
        return defaut

    if debut_arr != -1 and (debut_obj == -1 or debut_arr < debut_obj):
        ouvrant, fermant = "[", "]"
        debut = debut_arr
    else:
        ouvrant, fermant = "{", "}"
        debut = debut_obj

    clean = clean[debut:]
    fin = clean.rfind(fermant)
    if fin != -1:
        clean = clean[:fin + 1]

    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"[{contexte}] ⚠️ Parse échoué ({e}) — réparation…")

    # Réparation 1 : virgules traînantes
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", clean))
    except json.JSONDecodeError:
        pass

    # Réparation 2 : ne garder que les objets complets (cas de troncature)
    objets = re.findall(r"\{[^{}]*\}", clean)
    if objets:
        try:
            liste = json.loads("[" + ",".join(objets) + "]")
            print(f"[{contexte}] ✅ Réparé : {len(objets)} objet(s) récupéré(s)")
            if ouvrant == "[":
                return liste
            # On tente de replacer la liste sous sa clé d'origine
            cle = re.search(r'"(\w+)"\s*:\s*\[', clean)
            if cle:
                return {cle.group(1): liste}
            return liste
        except json.JSONDecodeError:
            pass

    print(f"[{contexte}] ❌ Réparation impossible")
    return defaut


# MARK: — Endpoints
@app.get("/")
def root():
    return {"message": "NutriScan API v1", "status": "running"}

@app.get("/health")
def health():
    return {"status": "healthy", "service": "nutriscan", "version": "1.0.0"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    """Politique de confidentialité — URL requise par App Store Connect."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NutriScan — Politique de confidentialité</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       max-width:720px;margin:0 auto;padding:32px 20px;line-height:1.65;color:#1c1c1e}
  h1{font-size:1.7rem;margin-bottom:.2em}
  h2{font-size:1.15rem;margin-top:2em;color:#166534}
  .maj{color:#6b7280;font-size:.9rem;margin-top:0}
  ul{padding-left:1.2em}
  li{margin-bottom:.4em}
  code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.9em}
  footer{margin-top:3em;padding-top:1em;border-top:1px solid #e5e7eb;
         color:#6b7280;font-size:.9rem}
</style>
</head>
<body>

<h1>Politique de confidentialité — NutriScan</h1>
<p class="maj">Dernière mise à jour : 29 août 2026</p>

<p>NutriScan analyse des photos de repas pour en estimer la valeur
nutritionnelle. Cette page décrit les données traitées et leur destination.</p>

<h2>Données conservées sur votre appareil</h2>
<p>Les informations suivantes restent stockées localement sur votre iPhone et
ne sont transmises à aucun serveur :</p>
<ul>
  <li>Les profils familiaux : prénom, âge, poids, sexe, objectif, photo</li>
  <li>L'historique des repas et les portions attribuées à chaque profil</li>
  <li>L'inventaire du réfrigérateur et les menus de cantine</li>
  <li>Votre base de plats personnelle</li>
</ul>
<p>La suppression de l'application efface définitivement ces données.
Aucune sauvegarde n'en est conservée de notre côté.</p>

<h2>Données transmises lors d'une analyse</h2>
<p>Quand vous analysez un repas, sont envoyés à notre serveur puis à
l'API Claude d'Anthropic :</p>
<ul>
  <li>La photo du repas</li>
  <li>L'âge, le poids, le sexe et l'objectif nutritionnel du profil actif</li>
  <li>Le poids estimé du plat et, le cas échéant, le nom que vous avez saisi</li>
  <li>Un identifiant anonyme servant à limiter le nombre d'analyses
      quotidiennes — il ne permet pas de vous identifier</li>
</ul>
<p>Anthropic traite ces données pour produire l'analyse. Consultez leur
politique de confidentialité sur <code>anthropic.com/privacy</code>.</p>

<h2>Codes-barres et étiquettes</h2>
<p>Quand vous scannez un code-barres, celui-ci est envoyé au service
Open Food Facts pour identifier le produit. Aucune donnée de profil ne
l'accompagne.</p>
<p>Quand vous photographiez une étiquette nutritionnelle, l'image est
transmise à notre serveur puis à l'API Claude d'Anthropic, uniquement
pour en lire les valeurs. Elle n'est pas conservée.</p>

<h2>Base de plats partagée</h2>
<p>Les résultats d'analyse (nom du plat, calories, macronutriments, score)
sont enregistrés dans une base commune à tous les utilisateurs, afin
d'éviter de réanalyser un plat déjà connu. Cette base ne contient
<strong>ni photo, ni donnée de profil, ni identifiant</strong> — uniquement
des informations nutritionnelles sur des plats.</p>

<h2>Corrections</h2>
<p>Si vous corrigez les valeurs nutritionnelles d'un plat, la correction
est transmise par courriel à l'administrateur pour validation. Elle
contient le nom du plat, les valeurs corrigées et un identifiant anonyme.</p>

<h2>Données de l'app Santé (HealthKit)</h2>
<p>Avec votre autorisation explicite, NutriScan lit une seule donnée de
l'app Santé d'Apple : <strong>les calories dépensées lors de vos
activités physiques</strong>. Elles sont affichées à côté de vos apports
alimentaires, pour en montrer le solde.</p>
<ul>
  <li>La lecture est <strong>ponctuelle</strong> : la donnée est affichée,
      jamais enregistrée dans NutriScan</li>
  <li>Elle n'est <strong>transmise à personne</strong> : ni à notre serveur,
      ni à Anthropic, ni à aucun tiers</li>
  <li>NutriScan <strong>n'écrit rien</strong> dans l'app Santé</li>
  <li>Vous pouvez retirer cette autorisation à tout moment :
      Réglages → Confidentialité et sécurité → Santé → NutriScan</li>
</ul>
<p>Refuser cet accès n'empêche aucune autre fonction de l'application.</p>

<h2>Sources de données nutritionnelles</h2>
<p>NutriScan s'appuie sur deux bases publiques, embarquées dans
l'application :</p>
<ul>
  <li><strong>Table Ciqual</strong> de l'Anses — composition nutritionnelle
      des aliments génériques</li>
  <li><strong>Open Food Facts</strong> — produits emballés, sous licence
      <em>Open Database License</em> (ODbL). Conformément à cette licence,
      la base dérivée utilisée par l'application est disponible sous ODbL
      sur simple demande à l'adresse de contact ci-dessous.</li>
</ul>
<p>Aucune de ces consultations ne nécessite de connexion : elles ont lieu
sur votre appareil.</p>

<h2>Ce que nous ne faisons pas</h2>
<ul>
  <li>Aucune publicité, aucun traceur publicitaire</li>
  <li>Aucune revente ni partage commercial de données, y compris celles
      issues de l'app Santé</li>
  <li>Aucun compte utilisateur, aucun mot de passe collecté</li>
  <li>Aucune géolocalisation</li>
</ul>

<h2>Conservation</h2>
<p>Les données de profil et l'historique demeurent sur votre appareil aussi
longtemps que l'application y est installée. Les informations
nutritionnelles de la base partagée sont conservées sans limite de durée,
n'étant rattachées à aucune personne.</p>

<h2>Vos droits</h2>
<p>Conformément au Règlement général sur la protection des données (RGPD),
vous disposez d'un droit d'accès, de rectification, d'effacement et
d'opposition. Les données locales s'effacent depuis l'application
(historique) ou en la désinstallant. Pour toute autre demande,
écrivez-nous à l'adresse ci-dessous.</p>

<h2>Enfants</h2>
<p>L'application permet de créer des profils pour des enfants, gérés par un
adulte. Les données de ces profils restent sur l'appareil et ne sont
transmises que dans le cadre décrit plus haut.</p>

<h2>Modifications</h2>
<p>Cette politique peut évoluer. La date en tête de page indique la
dernière révision.</p>

<h2>Contact</h2>
<p>Pour toute question relative à vos données : <code>haegel.s@hotmail.fr</code></p>

<footer>NutriScan — application indépendante</footer>

</body>
</html>""")


@app.get("/support", response_class=HTMLResponse)
def support():
    """Page d'assistance — URL requise par App Store Connect."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NutriScan — Assistance</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       max-width:720px;margin:0 auto;padding:32px 20px;line-height:1.65;color:#1c1c1e}
  h1{font-size:1.7rem;margin-bottom:.1em}
  .sous{color:#6b7280;margin-top:0}
  h2{font-size:1.15rem;margin-top:2em;color:#166534}
  .q{font-weight:600;margin-bottom:.2em;margin-top:1.2em}
  .contact{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;
           padding:18px 20px;margin:2em 0}
  code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.9em}
  a{color:#166534}
  footer{margin-top:3em;padding-top:1em;border-top:1px solid #e5e7eb;
         color:#6b7280;font-size:.9rem}
</style>
</head>
<body>

<h1>Assistance NutriScan</h1>
<p class="sous">Aide et contact</p>

<div class="contact">
  <strong>Une question, un problème ?</strong><br>
  Écrivez à <a href="mailto:haegel.s@hotmail.fr">haegel.s@hotmail.fr</a><br>
  <span style="color:#6b7280;font-size:.9rem">Réponse sous quelques jours.</span>
</div>

<h2>Questions fréquentes</h2>

<p class="q">Comment analyser un repas ?</p>
<p>Depuis l'onglet Analyser, prenez une photo ou choisissez-en une dans
votre galerie. Saisissez le nom du plat si vous le connaissez : l'analyse
sera nettement plus précise. Ajustez le poids estimé, puis lancez
l'analyse.</p>

<p class="q">Pourquoi le poids du plat est-il important ?</p>
<p>Toutes les valeurs nutritionnelles en découlent, ainsi que le calcul des
portions lorsqu'un plat est partagé. Un poids erroné fausse l'ensemble des
résultats.</p>

<p class="q">Comment partager un plat entre plusieurs personnes ?</p>
<p>Dans le calendrier, appuyez sur un repas. Indiquez la quantité en grammes
consommée par chaque membre. Les calories sont réparties proportionnellement
et apparaissent dans le calendrier de chacun.</p>

<p class="q">Comment ajouter un membre de la famille ?</p>
<p>Appuyez sur le sélecteur de profil, en haut de l'onglet Analyser. Vous
pouvez y créer, modifier ou supprimer des profils.</p>

<p class="q">Mes anciens repas affichent 0 kcal</p>
<p>Les repas enregistrés avant la mise à jour ne comportaient pas de données
caloriques. Seuls les repas analysés depuis affichent leurs valeurs.</p>

<p class="q">L'analyse échoue ou renvoie une erreur</p>
<p>Vérifiez votre connexion : l'analyse nécessite Internet. Le nombre
d'analyses est également limité chaque jour ; le compteur restant s'affiche
sous la photo.</p>

<p class="q">Comment scanner un ticket de caisse ?</p>
<p>Dans l'onglet Frigo, choisissez l'option PDF et sélectionnez le fichier
de votre ticket. Les produits alimentaires sont ajoutés automatiquement à
l'inventaire.</p>

<p class="q">Puis-je supprimer mes données ?</p>
<p>Le bouton corbeille du calendrier efface l'historique. Désinstaller
l'application supprime définitivement toutes les données locales.</p>

<h2>Signaler une erreur nutritionnelle</h2>
<p>Si les valeurs d'un plat vous semblent inexactes, utilisez le bouton
« Corriger ce plat » sous le résultat d'analyse. Les corrections sont
examinées avant d'être appliquées.</p>

<h2>Confidentialité</h2>
<p>Consultez la <a href="/privacy">politique de confidentialité</a>.</p>

<footer>NutriScan — application indépendante</footer>

</body>
</html>""")


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        quota = verifier_quota(req.user_id)
        if not quota["autorise"]:
            raise HTTPException(status_code=429, detail={"message": "Quota journalier atteint", "restants": 0, "maximum": quota["maximum"]})
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        indication_plat = ""
        if req.nom_plat:
            indication_plat = f"\nL'application a identifié ce plat comme étant : {req.nom_plat}. Utilise ce nom si tu es d'accord, sinon corrige-le.\n"
        prompt = f"""Tu es un expert en nutrition. Analyse la photo de ce repas et réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown).
Profil : {req.gender}, {req.age} ans, {req.weight} kg, objectif: {req.goal}.
Poids total du plat servi sur la photo : {req.poids_plat} grammes.
{indication_plat}
Retourne exactement ce format JSON :
{{"nom": "Nom du plat identifié", "description": "Description courte (1-2 phrases)", "score": 72, "verdict": "Titre du bilan", "commentaire": "Commentaire personnalisé (2-3 phrases)", "macros": {{"calories": 650, "proteines_g": 35, "glucides_g": 70, "lipides_g": 22}}, "ingredients": [{{"nom": "Riz blanc cuit", "grammes": 150, "calories": 195, "proteines_g": 4, "glucides_g": 42, "lipides_g": 1}}, {{"nom": "Poulet, blanc, cuit", "grammes": 120, "calories": 180, "proteines_g": 36, "glucides_g": 0, "lipides_g": 4}}], "nutrients": [{{"nom": "Protéines", "pct": 65, "niveau": "medium"}}, {{"nom": "Glucides", "pct": 85, "niveau": "good"}}, {{"nom": "Lipides", "pct": 45, "niveau": "low"}}, {{"nom": "Fibres", "pct": 30, "niveau": "low"}}, {{"nom": "Vitamines", "pct": 70, "niveau": "medium"}}, {{"nom": "Minéraux", "pct": 55, "niveau": "medium"}}], "conseils": ["Conseil 1", "Conseil 2", "Conseil 3"]}}

RÈGLES POUR « ingredients » :
- Décompose le plat en 2 à 8 ingrédients principaux, du plus lourd au plus léger.
- Nomme chaque ingrédient comme le ferait la table Ciqual de l'Anses :
  en français, générique, sans marque, avec l'état de cuisson quand il
  compte. « Riz blanc cuit », pas « Riz Uncle Ben's ». « Poulet, blanc,
  cuit », pas « escalope ». Ces noms servent à retrouver l'aliment dans
  une base de données : un nom de marque ou familier échouera.
- Les grammages doivent totaliser environ {req.poids_plat} g, mais ce sont
  surtout les PROPORTIONS entre ingrédients qui comptent : l'application
  les remettra à l'échelle du poids réel indiqué par l'utilisateur.
- Donne aussi les macros de chaque ingrédient POUR LE GRAMMAGE indiqué.
  Elles servent de valeur de repli quand l'ingrédient reste introuvable
  dans la base.
- La somme des macros des ingrédients doit rester cohérente avec « macros ».

Les valeurs macros doivent correspondre au poids total de {req.poids_plat}g."""
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        enregistrer_appel(req.user_id)
        result = parser_json_claude(response, defaut={}, contexte="analyze")
        if not result:
            raise HTTPException(status_code=502, detail="Réponse IA illisible, réessayez")
        result = normaliser_resultat(result)   # ✅ force les entiers pour Swift

        # Trace de la décomposition : c'est le seul moyen de vérifier que
        # Claude nomme les ingrédients comme la table Ciqual, condition
        # pour que la résolution locale fonctionne côté app.
        ingr = result.get("ingredients") or []
        if ingr:
            total = sum(i["grammes"] for i in ingr)
            print(f"[analyze] {len(ingr)} ingrédient(s), {total} g au total :")
            for i in ingr:
                print(f"[analyze]   {i['grammes']:>4} g  {i['nom']}")
        else:
            print("[analyze] ⚠️ aucun ingrédient dans la réponse")

        sauvegarder_plat_partage(result)
        result["quota"] = {"restants": MAX_ANALYSES_PAR_JOUR - len(user_analyses[req.user_id]), "maximum": MAX_ANALYSES_PAR_JOUR}
        return result
    except HTTPException:
        raise
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR DÉTAILLÉE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/plat/{nom}")
async def get_plat(nom: str):
    plat = chercher_plat_partage(nom)
    if not plat:
        raise HTTPException(status_code=404, detail="Plat non trouvé")
    return {
        "nom": plat.nom,
        "calories": plat.calories,
        "proteines_g": plat.proteines_g,
        "glucides_g": plat.glucides_g,
        "lipides_g": plat.lipides_g,
        "score": plat.score,
        "verdict": plat.verdict,
        "commentaire": plat.commentaire,
        "nutrients": json.loads(plat.nutrients) if plat.nutrients else [],
        "conseils": json.loads(plat.conseils) if plat.conseils else [],
        "description": "Plat reconnu depuis la base partagée",
        "macros": {"calories": plat.calories, "proteines_g": plat.proteines_g, "glucides_g": plat.glucides_g, "lipides_g": plat.lipides_g}
    }

@app.post("/correction")
async def soumettre_correction(req: CorrectionRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Base de données non disponible")
    session = Session()
    try:
        plat = chercher_plat_partage(req.nom_original)
        plat_id = plat.id if plat else None
        correction = CorrectionPending(id=str(uuid.uuid4()), plat_id=plat_id, nom_original=req.nom_original, nom_corrige=req.nom_corrige, calories_corrige=req.calories, proteines_corrige=req.proteines_g, glucides_corrige=req.glucides_g, lipides_corrige=req.lipides_g, user_id=req.user_id, statut="pending")
        session.add(correction)
        session.commit()
        envoyer_email_correction(correction.id, req.nom_original, req.nom_corrige, req.user_id)
        return {"success": True, "correction_id": correction.id, "message": "Correction soumise avec succès, merci !"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/correction/{user_id}/{nom_original}")
async def get_correction_utilisateur(user_id: str, nom_original: str):
    if not engine:
        return {"correction": None}
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.user_id == user_id, CorrectionPending.nom_original == nom_original, CorrectionPending.statut == "pending").first()
        if not correction:
            return {"correction": None}
        return {"correction": {"nom_corrige": correction.nom_corrige, "calories": correction.calories_corrige, "proteines_g": correction.proteines_corrige, "glucides_g": correction.glucides_corrige, "lipides_g": correction.lipides_corrige}}
    finally:
        session.close()

@app.get("/admin/valider/{correction_id}", response_class=HTMLResponse)
async def valider_correction(correction_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.id == correction_id).first()
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        if correction.statut != "pending":
            return HTMLResponse(f"<h1>ℹ️ Correction déjà traitée ({correction.statut})</h1>")
        if correction.plat_id:
            plat = session.query(PlatPartage).filter(PlatPartage.id == correction.plat_id).first()
            if plat:
                plat.nom = correction.nom_corrige
                plat.calories = correction.calories_corrige
                plat.proteines_g = correction.proteines_corrige
                plat.glucides_g = correction.glucides_corrige
                plat.lipides_g = correction.lipides_corrige
        else:
            nouveau = PlatPartage(id=str(uuid.uuid4()), nom=correction.nom_corrige, calories=correction.calories_corrige, proteines_g=correction.proteines_corrige, glucides_g=correction.glucides_corrige, lipides_g=correction.lipides_corrige, score=0, valide=True)
            session.add(nouveau)
        correction.statut = "validee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h1>✅ Correction validée !</h1><p>Le plat <strong>{correction.nom_corrige}</strong> a été mis à jour dans la base partagée.</p><p style="color:gray">Tous les utilisateurs bénéficieront de cette correction.</p></body></html>""")
    except Exception as e:
        session.rollback()
        return HTMLResponse(f"<h1>❌ Erreur : {str(e)}</h1>")
    finally:
        session.close()

@app.get("/admin/rejeter/{correction_id}", response_class=HTMLResponse)
async def rejeter_correction(correction_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.id == correction_id).first()
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        correction.statut = "rejetee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h1>❌ Correction rejetée</h1><p>La correction pour <strong>{correction.nom_original}</strong> a été rejetée.</p></body></html>""")
    finally:
        session.close()

@app.post("/suggestions")
async def suggestions(req: SuggestionsRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2000, messages=[{"role": "user", "content": req.prompt}])
        return {"result": response.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/next-meal")
async def next_meal(req: NextMealRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        nutrients_str = ", ".join([f"{n['nom']} à {n['pct']}%" for n in req.nutrients])
        frigo_str = ", ".join(req.aliments_frigo) if req.aliments_frigo else "aucune donnée disponible"
        prompt = f"""Tu es un expert en nutrition. L'utilisateur vient de manger : {req.nom_repas} (score: {req.score}/100).
Apports : {nutrients_str}.
Aliments disponibles : {frigo_str}.
Suggère UN SEUL repas idéal. Réponds UNIQUEMENT en JSON :
{{"nom": "Nom du repas", "description": "Description (1-2 phrases)", "raison": "Pourquoi ce repas complète le précédent", "ingredients": ["ingrédient 1", "ingrédient 2", "ingrédient 3"]}}"""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={}, contexte="next-meal")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-inventory")
async def scan_inventory(req: ScanInventoryRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = """Analyse cette photo et identifie tous les aliments visibles.
Réponds UNIQUEMENT en JSON :
{"aliments": [{ "nom": "Nom", "quantite": "500g", "categorie": "Légumes" }]}
Catégories : "Légumes", "Fruits", "Viandes/Poissons", "Produits laitiers", "Féculents", "Épicerie", "Boissons", "Autre"."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        return parser_json_claude(response, defaut={"aliments": []}, contexte="scan-inventory")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/recipe-from-inventory")
async def recipe_from_inventory(req: RecipeRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        aliments_str = ", ".join(req.aliments)
        consigne = f"\nUtilise obligatoirement : {req.aliment_principal}.\n" if req.aliment_principal else ""
        prompt = f"""Chef cuisinier spécialisé recettes simples. Aliments disponibles : {aliments_str}
{consigne}
Propose 3 recettes SIMPLES en JSON :
{{"recettes": [{{"nom": "Nom", "description": "Description", "temps_minutes": 20, "ingredients_utilises": ["ing1"], "ingredients_manquants": ["ing2"], "quantites": [{{"nom": "ing1", "grammes": 150}}]}}]}}

Pour "quantites" : le grammage de CHAQUE ingrédient utilisé, pour la recette entière (pas par personne). Un ingrédient en pièces — un œuf, une pomme — se note en grammes quand même (un œuf : 55 g, une pomme : 150 g).
Règles : max 5 ingrédients, au moins 1 légume/fruit, moins de 20 minutes."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2048, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={"recettes": []}, contexte="recipe")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-menu")
async def scan_menu(req: ScanMenuRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en lecture de menus de cantine scolaire.
Analyse cette photo de menu de cantine et extrais tous les plats par jour.
La semaine est : {req.semaine}
Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"semaine": "{req.semaine}", "jours": [{{"jour": "Lundi", "date": "2024-01-15", "plats": [{{"nom": "Carottes râpées", "type_plat": "entree"}}, {{"nom": "Poulet rôti", "type_plat": "plat"}}, {{"nom": "Haricots verts", "type_plat": "accompagnement"}}, {{"nom": "Yaourt", "type_plat": "dessert"}}]}}]}}
Types possibles : "entree", "plat", "accompagnement", "dessert", "laitage", "pain"
Inclus uniquement les jours de semaine (Lundi à Vendredi)."""
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        return parser_json_claude(response, defaut={"semaine": req.semaine, "jours": []}, contexte="scan-menu")
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR SCAN MENU: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/analyze-plat-cantine")
async def analyze_plat_cantine(req: AnalysePlatCantineRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en nutrition scolaire.
Estime les valeurs nutritionnelles d'une portion de cantine scolaire pour un enfant.
Plat : {req.nom_plat}
Type : {req.type_plat}
Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"nom": "{req.nom_plat}", "calories": 250, "proteines_g": 15, "glucides_g": 30, "lipides_g": 8, "score": 72, "verdict": "Bon apport nutritionnel", "conseils": ["Conseil 1", "Conseil 2"]}}
Base-toi sur une portion standard de cantine scolaire (portion enfant)."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={}, contexte="plat-cantine")
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR ANALYSE PLAT CANTINE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-receipt")
async def scan_receipt(req: ScanReceiptRequest):
    """Analyse un ticket de caisse via Claude"""
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = req.prompt if req.prompt else """Analyse ce ticket de caisse et retourne UNIQUEMENT:
[{"nom": "Produit", "quantite": "100g", "categorie": "Legumes"}]"""
        
        response = client.messages.create(
            model="claude-sonnet-4-5",
            # Sonnet plutôt que Haiku : les libellés de caisse sont des codes
            # — « 4TR JB S.C. LE TRANCHE FI », « BRK 1L PJ POMME FR CRF BI » —
            # et les décoder demande une vraie connaissance des enseignes.
            # Haiku suffisait pour Lidl, dont les noms sont presque en clair ;
            # il rendait les armes sur Carrefour. Un ticket est scanné une
            # fois par semaine : le surcoût est négligeable.
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw = response.content[0].text
        print(f"📄 Réponse brute: {len(raw)} chars (stop: {response.stop_reason})")
        if response.stop_reason == "max_tokens":
            print("⚠️ Réponse tronquée par max_tokens — réparation nécessaire")
        
        # Remove markdown
        clean = raw.replace("```json", "").replace("```", "").strip()
        
        # Replace smart quotes and dangerous chars
        clean = clean.replace(""", '"').replace(""", '"')
        clean = clean.replace("'", "'").replace("'", "'")
        clean = clean.replace("&amp;", "and")
        clean = clean.replace("&", "and")
        clean = clean.replace("'", "")
        
        # Extract the array [...] — même si le ] final manque (troncature)
        start = clean.find("[")
        if start == -1:
            print("❌ Aucun tableau trouvé")
            return {"aliments": []}
        
        clean = clean[start:]
        end = clean.rfind("]")
        if end != -1:
            clean = clean[:end + 1]
        print(f"✅ Tableau extrait: {len(clean)} chars")
        
        # Parse as array
        aliments = None
        try:
            aliments = json.loads(clean)
        except Exception as e:
            print(f"⚠️ Parse échoué ({e}) — tentative de réparation…")
            # ✅ RÉPARATION : garder uniquement les objets {...} complets
            objets = re.findall(r'\{[^{}]*\}', clean)
            if objets:
                repare = "[" + ",".join(objets) + "]"
                try:
                    aliments = json.loads(repare)
                    print(f"✅ Réparé : {len(objets)} objets complets récupérés")
                except Exception as e2:
                    print(f"❌ Réparation échouée : {e2}")
        
        if aliments is None:
            return {"aliments": []}
        
        # Ensure it's a list
        if not isinstance(aliments, list):
            aliments = []
        
        result = {
            "aliments": [
                {
                    "nom": str(item.get("nom", "")).strip(),
                    "quantite": str(item.get("quantite", "")).strip(),
                    "categorie": str(item.get("categorie", "")).strip()
                }
                for item in aliments
                if isinstance(item, dict) and item.get("nom")
            ]
        }
        
        print(f"✅ Result: {len(result['aliments'])} items found")
        return result
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return {"aliments": []}

@app.get("/quota/{user_id}")
def get_quota(user_id: str):
    return verifier_quota(user_id)

@app.get("/check")
def check():
    key = os.environ.get("ANTHROPIC_API_KEY", "NON TROUVÉE")
    db_ok = engine is not None
    return {"key_found": key != "NON TROUVÉE", "database_connected": db_ok}

@app.get("/test-email")
async def test_email():
    admin = os.environ.get("ADMIN_EMAIL", "NON CONFIGURÉ")
    api_key = os.environ.get("RESEND_API_KEY", "NON CONFIGURÉ")
    if not api_key or api_key == "NON CONFIGURÉ":
        return {"error": "RESEND_API_KEY manquante"}
    if not admin or admin == "NON CONFIGURÉ":
        return {"error": "ADMIN_EMAIL manquant"}
    try:
        resend.api_key = api_key
        response = resend.Emails.send({"from": "onboarding@resend.dev", "to": admin, "subject": "Test NutriScan", "html": "<h1>Test email NutriScan ✅</h1><p>Si vous recevez cet email, la configuration est correcte !</p>"})
        return {"success": True, "response": str(response)}
    except Exception as e:
        return {"error": str(e)}


class ScoreAliment(Base):
    """Score nutritionnel d'un aliment Ciqual, pour 100 g.

    Clé = alim_code de l'Anses : exact et stable, contrairement au
    rapprochement flou sur les mots qui confondrait « Pomme » et
    « Pomme de terre ». Table distincte de plats_partages, qui contient
    des plats composés dont les macros portent sur une portion entière.
    """
    __tablename__ = "scores_aliments"
    source_code = Column(String, primary_key=True)
    nom = Column(String, nullable=False)
    score = Column(Integer, default=0)
    verdict = Column(String, default="")
    commentaire = Column(Text, default="")
    conseils = Column(Text, default="[]")
    date_creation = Column(DateTime, default=datetime.utcnow)
    nombre_demandes = Column(Integer, default=1)


if engine:
    Base.metadata.create_all(engine)


class ScoreAlimentRequest(BaseModel):
    source_code: str
    nom: str
    calories: int = 0
    proteines_g: int = 0
    glucides_g: int = 0
    lipides_g: int = 0
    fibres_g: int = 0


@app.post("/score-aliment")
async def score_aliment(req: ScoreAlimentRequest, force: bool = False):
    """Note un aliment sur 100 g. Claude n'est appelé qu'une fois par
    aliment, tous utilisateurs confondus : le score suivant vient du cache.
    """
    defaut = {"score": 0, "verdict": "", "commentaire": "", "conseils": [], "cache": False}

    if not req.source_code:
        raise HTTPException(status_code=400, detail="source_code requis")

    # ---- 1. Le cache -------------------------------------------------
    if engine and not force:
        session = Session()
        try:
            connu = session.query(ScoreAliment).filter(
                ScoreAliment.source_code == req.source_code).first()
            if connu:
                connu.nombre_demandes += 1
                session.commit()
                print(f"[score-aliment] cache : {connu.nom} = {connu.score}")
                return {
                    "score": connu.score,
                    "verdict": connu.verdict,
                    "commentaire": connu.commentaire,
                    "conseils": json.loads(connu.conseils) if connu.conseils else [],
                    "cache": True,
                }
        except Exception as e:
            print(f"[score-aliment] lecture cache impossible : {e}")
        finally:
            session.close()

    # ---- 2. Claude ---------------------------------------------------
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en nutrition. Note la qualité nutritionnelle
de cet aliment brut, pour 100 grammes. La note porte sur l'aliment lui-même,
sa densité nutritionnelle — pas sur la quantité consommée.

Aliment : {req.nom}
Pour 100 g : {req.calories} kcal, {req.proteines_g} g de protéines,
{req.glucides_g} g de glucides, {req.lipides_g} g de lipides,
{req.fibres_g} g de fibres.

Ces chiffres viennent de la table Ciqual de l'Anses et ne couvrent que
les macronutriments. Ne pénalise PAS l'aliment pour les données absentes
et ne commente pas leur absence : appuie-toi sur ta connaissance de cet
aliment pour les vitamines, minéraux et le degré de transformation.
Une valeur à 0 peut signifier « non renseigné » et non « absent ».

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"score": 85, "verdict": "Excellent choix", "commentaire": "Deux phrases sur l'intérêt nutritionnel de cet aliment.", "conseils": ["Conseil 1", "Conseil 2"]}}

Le score va de 0 à 100. Un aliment brut peu transformé et riche en
micronutriments mérite une note élevée même s'il est calorique."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        resultat = parser_json_claude(response, defaut=dict(defaut), contexte="score-aliment")
    except Exception as e:
        print(f"[score-aliment] Claude indisponible : {e}")
        raise HTTPException(status_code=502, detail="Notation indisponible")

    resultat["score"] = _to_int(resultat.get("score"))
    resultat["verdict"] = str(resultat.get("verdict", "")).strip()
    resultat["commentaire"] = str(resultat.get("commentaire", "")).strip()
    resultat["conseils"] = [str(c).strip() for c in (resultat.get("conseils") or []) if str(c).strip()]

    if not resultat["score"]:
        raise HTTPException(status_code=502, detail="Réponse IA illisible")

    # ---- 3. Mémoriser pour tout le monde -----------------------------
    if engine:
        session = Session()
        try:
            session.merge(ScoreAliment(
                source_code=req.source_code,
                nom=req.nom,
                score=resultat["score"],
                verdict=resultat["verdict"],
                commentaire=resultat["commentaire"],
                conseils=json.dumps(resultat["conseils"], ensure_ascii=False),
                nombre_demandes=1,
            ))
            session.commit()
            print(f"[score-aliment] mémorisé : {req.nom} = {resultat['score']}")
        except Exception as e:
            session.rollback()
            print(f"[score-aliment] écriture impossible : {e}")
        finally:
            session.close()

    resultat["cache"] = False
    return resultat


class LireEtiquetteRequest(BaseModel):
    image_base64: str


@app.post("/lire-etiquette")
async def lire_etiquette(req: LireEtiquetteRequest):
    """Lit le tableau nutritionnel d'une étiquette photographiée.

    Tâche de lecture de texte, pas de reconnaissance de plat : c'est
    fiable, contrairement à l'identification visuelle d'une poudre en pot.
    Les valeurs restent proposées à l'utilisateur, jamais enregistrées
    d'office — une virgule mal lue fausserait tout un suivi.
    """
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = """Tu lis l'étiquette d'un produit alimentaire emballé.

Extrais le nom du produit, sa marque, son conditionnement et le tableau
des valeurs nutritionnelles.

PIÈGE IMPORTANT : beaucoup d'étiquettes affichent DEUX colonnes, « pour
100 g » et « par portion ». Retourne TOUJOURS les valeurs pour 100 g.
Si l'étiquette ne donne que les valeurs par portion, convertis-les pour
100 g et indique la masse de la portion dans « portion_g ».

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{"nom": "Whey Native", "marque": "Nutripure", "quantite": "900 g", "categorie": "Épicerie", "calories": 380, "proteines_g": 78, "glucides_g": 5, "lipides_g": 4, "portion_g": 30, "converti": false, "lisible": true}

Règles :
- "categorie" parmi : "Boissons", "Produits laitiers", "Viandes/Poissons",
  "Fruits", "Légumes", "Féculents", "Épicerie", "Autre".
- "converti" vaut true si tu as dû ramener des valeurs de portion à 100 g.
- "portion_g" vaut 0 si l'étiquette n'indique aucune portion.
- Si le tableau est illisible ou absent, renvoie "lisible": false et des
  valeurs à 0 : mieux vaut ne rien proposer qu'un chiffre inventé.
- N'invente jamais une valeur absente de l'étiquette : mets 0."""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/jpeg",
                                             "data": req.image_base64}},
                {"type": "text", "text": prompt},
            ]}],
        )

        resultat = parser_json_claude(response, defaut={}, contexte="lire-etiquette")
        if not resultat:
            raise HTTPException(status_code=502, detail="Étiquette illisible")

        propre = {
            "nom": str(resultat.get("nom", "")).strip(),
            "marque": str(resultat.get("marque", "")).strip(),
            "quantite": str(resultat.get("quantite", "")).strip(),
            "categorie": str(resultat.get("categorie", "Autre")).strip() or "Autre",
            "calories": _to_int(resultat.get("calories")),
            "proteines_g": _to_int(resultat.get("proteines_g")),
            "glucides_g": _to_int(resultat.get("glucides_g")),
            "lipides_g": _to_int(resultat.get("lipides_g")),
            "portion_g": _to_int(resultat.get("portion_g")),
            "converti": bool(resultat.get("converti", False)),
            "lisible": bool(resultat.get("lisible", True)),
        }

        print(f"[lire-etiquette] {propre['nom'] or '(sans nom)'} — "
              f"{propre['calories']} kcal/100 g"
              + (" (converti depuis une portion)" if propre["converti"] else "")
              + ("" if propre["lisible"] else " — ÉTIQUETTE ILLISIBLE"))
        return propre

    except HTTPException:
        raise
    except Exception as e:
        print(f"[lire-etiquette] erreur : {e}")
        raise HTTPException(status_code=500, detail=str(e))


class AlimentGenerique(Base):
    """Aliments obtenus par leur nom, pour 100 g.

    Table distincte de plats_partages, qui stocke des PORTIONS entières :
    mélanger les deux unités a déjà produit des calories fausses. Ici
    tout est ramené à 100 g, comme Ciqual.

    Couvre ce que Ciqual ignore : les marques (Big Mac), les plats
    composés (bo bun au porc), les recettes du quotidien.
    """
    __tablename__ = "aliments_generiques"
    nom_normalise = Column(String, primary_key=True)
    nom = Column(String, nullable=False)
    calories = Column(Integer, default=0)
    proteines_g = Column(Integer, default=0)
    glucides_g = Column(Integer, default=0)
    lipides_g = Column(Integer, default=0)
    fibres_g = Column(Integer, default=0)
    portion_g = Column(Integer, default=0)
    portion_libelle = Column(String, default="")
    date_creation = Column(DateTime, default=datetime.utcnow)
    nombre_demandes = Column(Integer, default=1)


if engine:
    Base.metadata.create_all(engine)


class AlimentGeneriqueRequest(BaseModel):
    nom: str


def normaliser_nom(texte: str) -> str:
    """Même normalisation que côté app : accents, ligatures, ponctuation."""
    import unicodedata
    t = texte.lower().replace("\u0153", "oe").replace("\u00e6", "ae")
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[,;:/()'\u2019\"_-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@app.post("/aliment-generique")
async def aliment_generique(req: AlimentGeneriqueRequest, force: bool = False):
    """Valeurs nutritionnelles d'un aliment nommé, POUR 100 G.

    Claude n'est appelé qu'une fois par aliment : le suivant vient du
    cache, pour tous les utilisateurs.
    """
    nom = (req.nom or "").strip()
    if len(nom) < 3:
        raise HTTPException(status_code=400, detail="Nom trop court")

    cle = normaliser_nom(nom)

    # ---- 1. Le cache ----------------------------------------------------
    if engine and not force:
        session = Session()
        try:
            connu = session.query(AlimentGenerique).filter(
                AlimentGenerique.nom_normalise == cle).first()
            if connu:
                connu.nombre_demandes += 1
                session.commit()
                print(f"[aliment-generique] cache : {connu.nom} = {connu.calories} kcal/100 g")
                return {
                    "nom": connu.nom,
                    "calories": connu.calories,
                    "proteines_g": connu.proteines_g,
                    "glucides_g": connu.glucides_g,
                    "lipides_g": connu.lipides_g,
                    "fibres_g": connu.fibres_g,
                    "portion_g": connu.portion_g,
                    "portion_libelle": connu.portion_libelle or "",
                    "cache": True,
                }
        except Exception as e:
            print(f"[aliment-generique] lecture cache impossible : {e}")
        finally:
            session.close()

    # ---- 2. Claude ------------------------------------------------------
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Donne les valeurs nutritionnelles de cet aliment ou plat,
POUR 100 GRAMMES.

Aliment : {nom}

Il peut s'agir d'un produit de marque (Big Mac, Nutella), d'un plat
composé (bo bun au porc, poulet basquaise), ou d'une préparation
courante. Appuie-toi sur les valeurs publiées par le fabricant quand
elles existent, sinon sur une recette standard.

Indique aussi une PORTION USUELLE : le poids d'une part telle qu'elle
est réellement servie. Un Big Mac pèse environ 219 g, un bol de bo bun
environ 450 g.

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"nom": "Big Mac", "calories": 240, "proteines_g": 12, "glucides_g": 19, "lipides_g": 12, "fibres_g": 2, "portion_g": 219, "portion_libelle": "1 sandwich"}}

Règles :
- Toutes les valeurs sont POUR 100 G, sauf portion_g.
- Nomme l'aliment correctement, sans reprendre les fautes de la saisie.
- Si le nom ne désigne aucun aliment identifiable, renvoie
  {{"calories": 0}} : mieux vaut ne rien proposer qu'inventer."""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        resultat = parser_json_claude(response, defaut={}, contexte="aliment-generique")
    except Exception as e:
        print(f"[aliment-generique] Claude indisponible : {e}")
        raise HTTPException(status_code=502, detail="Recherche indisponible")

    propre = {
        "nom": str(resultat.get("nom") or nom).strip(),
        "calories": _to_int(resultat.get("calories")),
        "proteines_g": _to_int(resultat.get("proteines_g")),
        "glucides_g": _to_int(resultat.get("glucides_g")),
        "lipides_g": _to_int(resultat.get("lipides_g")),
        "fibres_g": _to_int(resultat.get("fibres_g")),
        "portion_g": _to_int(resultat.get("portion_g")),
        "portion_libelle": str(resultat.get("portion_libelle") or "").strip(),
    }

    if propre["calories"] <= 0:
        print(f"[aliment-generique] « {nom} » non identifié")
        raise HTTPException(status_code=404, detail="Aliment non identifié")

    # ---- 3. Mémoriser pour tout le monde --------------------------------
    if engine:
        session = Session()
        try:
            session.merge(AlimentGenerique(nom_normalise=cle, **propre,
                                           nombre_demandes=1))
            session.commit()
            print(f"[aliment-generique] mémorisé : {propre['nom']} = "
                  f"{propre['calories']} kcal/100 g, portion {propre['portion_g']} g")
        except Exception as e:
            session.rollback()
            print(f"[aliment-generique] écriture impossible : {e}")
        finally:
            session.close()

    propre["cache"] = False
    return propre


class AlimentPropose(Base):
    """Aliment saisi à la main par un utilisateur, en attente de validation.

    Séparé d'aliments_generiques : une saisie non vérifiée ne doit pas
    servir aux autres utilisateurs. Elle est utilisable immédiatement par
    celui qui l'a saisie, localement, mais n'entre dans la base commune
    qu'après validation.
    """
    __tablename__ = "aliments_proposes"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nom = Column(String, nullable=False)
    nom_normalise = Column(String, nullable=False)
    calories = Column(Integer, default=0)
    proteines_g = Column(Integer, default=0)
    glucides_g = Column(Integer, default=0)
    lipides_g = Column(Integer, default=0)
    fibres_g = Column(Integer, default=0)
    portion_g = Column(Integer, default=0)
    portion_libelle = Column(String, default="")
    user_id = Column(String, default="")
    statut = Column(String, default="pending")
    date_soumission = Column(DateTime, default=datetime.utcnow)


if engine:
    Base.metadata.create_all(engine)


class AlimentProposeRequest(BaseModel):
    nom: str
    calories: int
    proteines_g: int = 0
    glucides_g: int = 0
    lipides_g: int = 0
    fibres_g: int = 0
    portion_g: int = 0
    portion_libelle: str = ""
    user_id: str = "anonymous"


def envoyer_email_aliment(propose: "AlimentPropose"):
    if not resend.api_key or not ADMIN_EMAIL:
        print("⚠️ Email non configuré — validation impossible par courriel")
        return

    base = "https://web-production-c1f45.up.railway.app"
    valider = f"{base}/admin/valider-aliment/{propose.id}"
    rejeter = f"{base}/admin/rejeter-aliment/{propose.id}"

    try:
        resend.Emails.send({
            "from": "nutriscan@resend.dev",
            "to": ADMIN_EMAIL,
            "subject": f"NutriScan — Nouvel aliment à valider : {propose.nom}",
            "html": f"""
            <h2>Nouvel aliment proposé</h2>
            <p><strong>{propose.nom}</strong> — valeurs pour 100 g</p>
            <table cellpadding="6" style="border-collapse:collapse">
              <tr><td>Calories</td><td><strong>{propose.calories} kcal</strong></td></tr>
              <tr><td>Protéines</td><td>{propose.proteines_g} g</td></tr>
              <tr><td>Glucides</td><td>{propose.glucides_g} g</td></tr>
              <tr><td>Lipides</td><td>{propose.lipides_g} g</td></tr>
              <tr><td>Fibres</td><td>{propose.fibres_g} g</td></tr>
              <tr><td>Portion</td><td>{propose.portion_g} g — {propose.portion_libelle or "non précisée"}</td></tr>
            </table>
            <p style="color:#666;font-size:13px">Proposé par {propose.user_id[:8]}…</p>
            <br>
            <a href="{valider}" style="background:#22c55e;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;margin-right:12px">
                ✅ Valider
            </a>
            <a href="{rejeter}" style="background:#ef4444;color:white;padding:12px 24px;border-radius:6px;text-decoration:none">
                ❌ Rejeter
            </a>
            """
        })
        print(f"📧 Email envoyé pour l'aliment {propose.nom}")
    except Exception as e:
        print(f"❌ Erreur email : {e}")


@app.post("/aliment-propose")
async def aliment_propose(req: AlimentProposeRequest):
    """Enregistre un aliment saisi à la main, en attente de validation."""
    nom = (req.nom or "").strip()
    if len(nom) < 3:
        raise HTTPException(status_code=400, detail="Nom trop court")
    if req.calories <= 0:
        raise HTTPException(status_code=400, detail="Calories requises")
    if not engine:
        raise HTTPException(status_code=503, detail="Base de données indisponible")

    session = Session()
    try:
        propose = AlimentPropose(
            id=str(uuid.uuid4()),
            nom=nom,
            nom_normalise=normaliser_nom(nom),
            calories=_to_int(req.calories),
            proteines_g=_to_int(req.proteines_g),
            glucides_g=_to_int(req.glucides_g),
            lipides_g=_to_int(req.lipides_g),
            fibres_g=_to_int(req.fibres_g),
            portion_g=_to_int(req.portion_g),
            portion_libelle=(req.portion_libelle or "").strip(),
            user_id=req.user_id,
            statut="pending",
        )
        session.add(propose)
        session.commit()
        session.refresh(propose)
        envoyer_email_aliment(propose)
        print(f"[aliment-propose] {nom} — {propose.calories} kcal/100 g, en attente")
        return {"success": True, "id": propose.id,
                "message": "Aliment enregistré. Il sera partagé après validation."}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/admin/valider-aliment/{propose_id}", response_class=HTMLResponse)
async def valider_aliment(propose_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        p = session.query(AlimentPropose).filter(
            AlimentPropose.id == propose_id).first()
        if not p:
            return HTMLResponse("<h1>❌ Proposition introuvable</h1>")
        if p.statut != "pending":
            return HTMLResponse(f"<h1>ℹ️ Déjà traitée ({p.statut})</h1>")

        # merge : une proposition peut corriger un aliment déjà connu
        session.merge(AlimentGenerique(
            nom_normalise=p.nom_normalise,
            nom=p.nom,
            calories=p.calories,
            proteines_g=p.proteines_g,
            glucides_g=p.glucides_g,
            lipides_g=p.lipides_g,
            fibres_g=p.fibres_g,
            portion_g=p.portion_g,
            portion_libelle=p.portion_libelle,
            nombre_demandes=1,
        ))
        p.statut = "validee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center">
        <h1>✅ Aliment validé</h1>
        <p><strong>{p.nom}</strong> — {p.calories} kcal pour 100 g</p>
        <p style="color:gray">Il est désormais disponible pour tous les utilisateurs.</p>
        </body></html>""")
    except Exception as e:
        session.rollback()
        return HTMLResponse(f"<h1>❌ Erreur : {str(e)}</h1>")
    finally:
        session.close()


@app.get("/admin/rejeter-aliment/{propose_id}", response_class=HTMLResponse)
async def rejeter_aliment(propose_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        p = session.query(AlimentPropose).filter(
            AlimentPropose.id == propose_id).first()
        if not p:
            return HTMLResponse("<h1>❌ Proposition introuvable</h1>")
        p.statut = "rejetee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center">
        <h1>❌ Proposition rejetée</h1>
        <p><strong>{p.nom}</strong> n'entrera pas dans la base commune.</p>
        <p style="color:gray">L'utilisateur conserve sa saisie sur son appareil.</p>
        </body></html>""")
    finally:
        session.close()


class SuggererRepasRequest(BaseModel):
    """Contexte pour proposer des repas.

    Tout ce qui permet à Claude de proposer juste : ce qu'il y a dans le
    frigo, ce qui a été mangé récemment (pour varier), ce qui reste à
    consommer aujourd'hui (pour cadrer), et le moment de la journée.
    """
    aliments_frigo: list[str] = []
    repas_recents: list[str] = []
    categorie: str = "Déjeuner"
    calories_restantes: int = 0
    proteines_restantes: int = 0
    glucides_restants: int = 0
    lipides_restants: int = 0
    age: int = 30
    gender: str = "homme"
    goal: str = "équilibré"
    user_id: str = "anonymous"
    nombre_personnes: int = 1


@app.post("/suggerer-repas")
async def suggerer_repas(req: SuggererRepasRequest):
    """Trois propositions de repas, structurées comme une analyse.

    Même contrat que /analyze pour les ingrédients : nommés à la façon
    de Ciqual, avec grammages et macros. C'est ce qui permet à l'app de
    les résoudre localement et de les enregistrer comme n'importe quel
    repas — modifiables, partageables, avec leur composition.
    """
    frigo = ", ".join(req.aliments_frigo) if req.aliments_frigo else "rien de renseigné"
    recents = ", ".join(req.repas_recents) if req.repas_recents else "aucun"

    # Sans budget restant, on cadre sur une portion raisonnable du moment
    cible = req.calories_restantes if req.calories_restantes > 150 else {
        "Petit-déjeuner": 450, "Déjeuner": 700, "Dîner": 600, "Collation": 200,
    }.get(req.categorie, 600)

    prompt = f"""Tu es un nutritionniste qui compose des repas concrets.

CONTEXTE
- Pour : {req.nombre_personnes} personne(s)
- Moment : {req.categorie}
- Dans le frigo : {frigo}
- Mangé ces derniers jours : {recents}
- Profil : {req.gender}, {req.age} ans, objectif « {req.goal} »
- Il reste aujourd'hui environ {cible} kcal
  (protéines {req.proteines_restantes} g, glucides {req.glucides_restants} g,
   lipides {req.lipides_restants} g)

CONSIGNES
1. Propose TROIS repas différents, adaptés au moment de la journée.
2. SOBRIÉTÉ : de 3 à 5 ingrédients par repas, pas plus. Le frigo est
   une réserve où PUISER, pas une liste à ÉPUISER. Un bon repas tient
   en une protéine, un féculent ou légume, une matière grasse, et un
   ou deux compléments. N'ajoute un ingrédient que s'il apporte quelque
   chose que les autres n'apportent pas.
3. Puise dans le frigo. Un ingrédient absent est permis s'il est
   courant (huile, sel, un œuf), pas s'il faut aller l'acheter.
4. Évite de reproduire les repas récents : c'est la variété qu'on
   cherche.
5. ÉQUILIBRE avec peu d'éléments : vise les protéines, glucides et
   lipides restants, et pense aux micronutriments — un légume coloré
   ou un fruit couvre les vitamines mieux qu'un troisième féculent.
   Vise le budget calorique restant sans le dépasser ; si les protéines
   manquent, compense ; si les lipides sont déjà hauts, allège.
6. PORTIONS réalistes, pour {req.nombre_personnes} personne(s). Une
   part adulte fait 350 à 500 g au total ; multiplie par le nombre de
   personnes, PAS PLUS. Pour une personne seule : 120 à 150 g de
   protéine, 150 à 200 g de féculent cuit, 100 à 150 g de légumes, une
   cuillère de matière grasse. Un plat à 800 g pour une personne est une
   erreur, pas une générosité.
7. Décompose chaque repas en INGRÉDIENTS, nommés comme dans la table
   Ciqual de l'Anses : en français, génériques, avec l'état de cuisson.
   « Riz blanc, cuit », « Blanc de poulet, rôti », « Huile d'olive ».
   Les grammages doivent totaliser le poids du repas.

Les macros, le poids et les grammages d'ingrédients portent sur le
TOTAL pour {req.nombre_personnes} personne(s).

Réponds UNIQUEMENT en JSON valide, sans backticks :
{{"propositions": [
  {{"nom": "Nom court du repas",
    "description": "Une phrase : ce que c'est et pourquoi ça convient",
    "poids_g": 420,
    "score": 82,
    "macros": {{"calories": 610, "proteines_g": 38, "glucides_g": 62, "lipides_g": 18}},
    "ingredients": [
      {{"nom": "Riz blanc, cuit", "grammes": 150, "calories": 195, "proteines_g": 4, "glucides_g": 42, "lipides_g": 0}}
    ]
  }}
]}}"""

    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        brut = parser_json_claude(response, defaut={"propositions": []},
                                  contexte="suggerer-repas")
    except Exception as e:
        print(f"[suggerer-repas] Claude indisponible : {e}")
        raise HTTPException(status_code=502, detail="Suggestions indisponibles")

    propositions = []
    for p in (brut.get("propositions") or [])[:3]:
        macros = p.get("macros") or {}
        ingredients = []
        for i in (p.get("ingredients") or []):
            nom = str(i.get("nom") or "").strip()
            if not nom:
                continue
            ingredients.append({
                "nom": nom,
                "grammes": _to_int(i.get("grammes")),
                "calories": _to_int(i.get("calories")),
                "proteines_g": _to_int(i.get("proteines_g")),
                "glucides_g": _to_int(i.get("glucides_g")),
                "lipides_g": _to_int(i.get("lipides_g")),
            })
        propositions.append({
            "nom": str(p.get("nom") or "Repas").strip(),
            "description": str(p.get("description") or "").strip(),
            "poids_g": _to_int(p.get("poids_g")) or sum(i["grammes"] for i in ingredients),
            "score": max(0, min(100, _to_int(p.get("score"), 70))),
            "macros": {
                "calories": _to_int(macros.get("calories")),
                "proteines_g": _to_int(macros.get("proteines_g")),
                "glucides_g": _to_int(macros.get("glucides_g")),
                "lipides_g": _to_int(macros.get("lipides_g")),
            },
            "ingredients": ingredients,
        })

    print(f"[suggerer-repas] {len(propositions)} proposition(s) pour {req.categorie}, "
          f"frigo {len(req.aliments_frigo)} article(s)")
    return {"propositions": propositions}


class SuggererCoursesRequest(BaseModel):
    """Contexte pour proposer une liste de courses."""
    frigo: list[str] = []
    repas_recents: list[str] = []
    calories_moyennes: int = 0
    proteines_moyennes: int = 0
    glucides_moyens: int = 0
    lipides_moyens: int = 0
    cible_calories: int = 0
    cible_proteines: int = 0
    cible_glucides: int = 0
    cible_lipides: int = 0
    nombre_personnes: int = 1
    age: int = 30
    gender: str = "homme"
    goal: str = "équilibré"


@app.post("/suggerer-courses")
async def suggerer_courses(req: SuggererCoursesRequest):
    """Une liste de courses pour équilibrer et varier.

    Raisonne sur l'écart entre ce qui est mangé et ce qui devrait l'être,
    et sur ce qui manque au frigo pour y remédier. Chaque article vient
    avec sa raison — c'est elle qui rend la liste utile plutôt que
    prescriptive.
    """
    frigo = ", ".join(req.frigo) if req.frigo else "vide"
    recents = ", ".join(req.repas_recents) if req.repas_recents else "aucun repas enregistré"

    ecart = ""
    if req.cible_calories > 0 and req.calories_moyennes > 0:
        ecart = f"""
- Sur les deux dernières semaines, apports moyens par jour :
  {req.calories_moyennes} kcal (cible {req.cible_calories}),
  protéines {req.proteines_moyennes} g (cible {req.cible_proteines}),
  glucides {req.glucides_moyens} g (cible {req.cible_glucides}),
  lipides {req.lipides_moyens} g (cible {req.cible_lipides})"""

    prompt = f"""Tu es un nutritionniste qui aide à faire ses courses.

CONTEXTE
- Foyer : {req.nombre_personnes} personne(s), profil {req.gender}, {req.age} ans,
  objectif « {req.goal} »
- Au frigo aujourd'hui : {frigo}
- Repas des deux dernières semaines : {recents}{ecart}

CONSIGNES
1. Propose de 6 à 10 ARTICLES à acheter, pas plus. Une liste de courses
   n'est pas un inventaire d'épicerie.
2. Chaque article répond à un besoin précis, à dire en une phrase :
   « Il manque une source de fibres », « Aucun poisson gras ces deux
   dernières semaines », « Les légumes verts sont absents du frigo ».
3. VARIÉTÉ : privilégie ce qui n'a PAS été mangé récemment et ce qui
   N'EST PAS déjà au frigo. Ne propose pas ce qu'on a en quantité.
4. ÉQUILIBRE : couvre les manques visibles — un macronutriment en
   dessous de la cible, une famille absente (légumineuses, poisson,
   fruits, produits laitiers, céréales complètes).
5. Reste RÉALISTE : des aliments de supermarché courants, pas des
   produits rares. Une quantité indicative pour le foyer et la semaine.
6. Groupe par rayon : Fruits et légumes, Viandes et poissons, Produits
   laitiers, Épicerie, Boissons.

Réponds UNIQUEMENT en JSON valide, sans backticks :
{{"articles": [
  {{"nom": "Lentilles vertes", "quantite": "500 g", "rayon": "Épicerie",
    "raison": "Aucune légumineuse ces deux dernières semaines ; fibres et protéines végétales."}}
]}}"""

    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        brut = parser_json_claude(response, defaut={"articles": []},
                                  contexte="suggerer-courses")
    except Exception as e:
        print(f"[suggerer-courses] Claude indisponible : {e}")
        raise HTTPException(status_code=502, detail="Suggestions indisponibles")

    articles = []
    for a in (brut.get("articles") or [])[:10]:
        nom = str(a.get("nom") or "").strip()
        if not nom:
            continue
        articles.append({
            "nom": nom,
            "quantite": str(a.get("quantite") or "").strip(),
            "rayon": str(a.get("rayon") or "Épicerie").strip(),
            "raison": str(a.get("raison") or "").strip(),
        })

    print(f"[suggerer-courses] {len(articles)} article(s), frigo {len(req.frigo)}, "
          f"repas {len(req.repas_recents)}")
    return {"articles": articles}

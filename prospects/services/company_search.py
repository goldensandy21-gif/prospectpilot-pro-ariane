import csv, io, re, time
import httpx
from openpyxl import Workbook

BASE_URL = "https://recherche-entreprises.api.gouv.fr/search"

def normalize_naf(value):
    value=(value or "").strip().upper().replace(" ","")
    compact=value.replace(".","")
    if re.fullmatch(r"\d{4}[A-Z]", compact): return f"{compact[:2]}.{compact[2:]}"
    return value

def _params(query="",naf_code="",postal_code="",department="",page=1,per_page=25):
    p={"page":max(1,int(page or 1)),"per_page":min(25,int(per_page or 25)),"etat_administratif":"A"}
    if query: p["q"]=query.strip()
    naf=normalize_naf(naf_code)
    if naf: p["activite_principale"]=naf
    if postal_code: p["code_postal"]=postal_code.strip()
    elif department: p["departement"]=department.strip().upper()
    return p,naf

def _band_min(code):
    return {"NN":0,"00":0,"01":1,"02":3,"03":6,"11":10,"12":20,"21":50,"22":100,"31":200,"32":250,"41":500,"42":1000,"51":2000,"52":5000,"53":10000}.get(code or "",0)

def convert(item, naf=""):
    siege=item.get("siege") or {}; cp=siege.get("code_postal") or ""; diffusion=(item.get("statut_diffusion") or "").upper(); partial=diffusion=="P"
    return {"name":item.get("nom_complet") or item.get("nom_raison_sociale") or "Entreprise","legal_name":item.get("nom_raison_sociale") or "","sector":item.get("libelle_activite_principale") or naf,"naf_code":item.get("activite_principale") or naf,"department":siege.get("departement") or cp[:2],"city":siege.get("libelle_commune") or "","postal_code":cp,"address":siege.get("adresse") or " ".join(filter(None,[siege.get("numero_voie"),siege.get("type_voie"),siege.get("libelle_voie")])),"siren":item.get("siren") or "","siret":siege.get("siret") or None,"employee_band":item.get("tranche_effectif_salarie") or siege.get("tranche_effectif_salarie") or "","creation_date":item.get("date_creation"),"diffusion_partial":partial,"prospecting_allowed":not partial,"source":"api_recherche_entreprises","source_url":"https://annuaire-entreprises.data.gouv.fr/entreprise/"+(item.get("siren") or ""),"source_payload":item}

def search_companies(query="",naf_code="",postal_code="",department="",city="",employee_min=None,page=1,per_page=25):
    params,naf=_params(query,naf_code,postal_code,department,page,per_page)
    with httpx.Client(timeout=30,headers={"Accept":"application/json","User-Agent":"ProspectPilot/3.0"}) as c: r=c.get(BASE_URL,params=params)
    if r.status_code==400: raise ValueError(f"Recherche invalide : {r.text}")
    r.raise_for_status(); payload=r.json(); out=[]
    for item in payload.get("results",[]):
        row=convert(item,naf)
        if city and city.lower().strip() not in row["city"].lower(): continue
        if employee_min not in (None,"") and _band_min(row["employee_band"])<int(employee_min): continue
        out.append(row)
    return {"results":out,"page":int(payload.get("page",page)),"per_page":int(payload.get("per_page",per_page)),"total_results":int(payload.get("total_results",len(out))),"total_pages":int(payload.get("total_pages",1))}

def fetch_all_companies(max_results=2000,**kwargs):
    first=search_companies(page=1,**kwargs); rows=list(first["results"]); pages=min(first["total_pages"],(max_results+24)//25)
    for n in range(2,pages+1):
        time.sleep(.18); rows.extend(search_companies(page=n,**kwargs)["results"])
        if len(rows)>=max_results: break
    return rows[:max_results]

def get_company_by_siren(siren):
    data=search_companies(query=siren,page=1)
    exact=[x for x in data["results"] if x["siren"]==siren]
    if not exact: raise LookupError("Entreprise introuvable")
    return exact[0]

def to_csv(rows):
    out=io.StringIO(); w=csv.writer(out); w.writerow(["Entreprise","Raison sociale","Secteur","NAF","Département","Ville","Code postal","Adresse","SIREN","SIRET","Effectif","Prospection"])
    for x in rows:w.writerow([x["name"],x["legal_name"],x["sector"],x["naf_code"],x["department"],x["city"],x["postal_code"],x["address"],x["siren"],x["siret"],x["employee_band"],"Oui" if x["prospecting_allowed"] else "Non"])
    return out.getvalue().encode("utf-8-sig")

def to_xlsx(rows):
    wb=Workbook(); ws=wb.active; ws.title="Entreprises"; heads=["Entreprise","Raison sociale","Secteur","NAF","Département","Ville","Code postal","Adresse","SIREN","SIRET","Effectif","Prospection"]; ws.append(heads)
    for x in rows: ws.append([x["name"],x["legal_name"],x["sector"],x["naf_code"],x["department"],x["city"],x["postal_code"],x["address"],x["siren"],x["siret"],x["employee_band"],"Oui" if x["prospecting_allowed"] else "Non"])
    out=io.BytesIO(); wb.save(out); return out.getvalue()

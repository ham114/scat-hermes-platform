"""Source-reviewed semantic rewrites of the 29 quarantined chart regions."""
import json
import re
import hashlib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/rag-chart-rewrite-20261124'
sources=json.loads((OUT/'source-inspection.json').read_text(encoding='utf8'))
authored={}
def put(i,title,units,kind='reviewed-table',notes=None):
    authored[i]={'title':title,'units':units,'kind':kind,'notes':notes or []}

put(0,'I062/500 Estimated Accuracies: XY covariance component',[
 'Two octets, bits 16 through 1: COV (XY Covariance Component). COV is encoded in two\'s complement form. Bit 1 is the least significant bit (LSB), with resolution 0.5 m. No extension indicator is shown inside these 16 bits.',
 'The encoded XY covariance component is sign{Cov(X,Y)} * sqrt{abs[Cov(X,Y)]}; it is not the untransformed covariance itself. The source note states a maximum value for the unsigned component of 16.383 km.'])
put(1,'I062/080 Track Status: Fifth Extent',[
 f'Fifth extent, bit {b} ({name}): 0 = {zero}; 1 = {one}.' for b,name,zero,one in [
 (8,'DUPT','Default value','Duplicate Mode 3/A Code'),(7,'DUPF','Default value','Duplicate Flight Plan'),
 (6,'DUPM','Default value','Duplicate Flight Plan due to manual correlation'),(5,'SFC','Default value','Surface target'),
 (4,'IDD','No indication','Duplicate Flight-ID'),(3,'IEC','Default value','Inconsistent Emergency Code'),
 (2,'MLAT','Default value','Age of the last received MLAT track update is higher than system dependent threshold'),
 (1,'FX','End of data item','Extension into next extent')]])
put(2,'I062/290 System Track Update Ages: Primary Subfield continuation',[
 'Bit 7 (LOP) indicates Subfield #9, Loop age: 0 = absent; 1 = present.',
 'Bit 6 (MLT) indicates Subfield #10, Multilateration age: 0 = absent; 1 = present.',
 'Bits 5 through 2 are spare and set to zero. Bit 1 (FX): 0 = no extension; 1 = extension.'])
put(3,'I062/340 Subfield #4: Last Measured Mode C Code',[
 'Two octets: bit 16 (V), bit 15 (G), bits 14 through 1 Last Measured Mode C Code.',
 'Bit 16 (V): 0 = Code validated; 1 = Code not validated. Bit 15 (G): 0 = Default; 1 = Garbled code.',
 'Bits 14 through 1 encode Last Measured Mode C Code in two\'s complement form. LSB = 1/4 FL; minimum = -12 FL; maximum = 1270 FL.'])
put(4,'I062/340 Subfield #5: Last Measured Mode 3/A Code',[
 'Two octets: bit 16 (V), bit 15 (G), bit 14 (L), bit 13 spare set to zero, bits 12 through 1 Mode 3/A reply.',
 'V: 0 = Code Validated; 1 = Code not Validated. G: 0 = Default; 1 = Garbled code.',
 'L: 0 = MODE 3/A code derived from the transponder reply; 1 = Smoothed MODE 3/A code provided by a sensor local tracker.',
 'Bits 12..1 are A4,A2,A1,B4,B2,B1,C4,C2,C1,D4,D2,D1, respectively, representing four octal digits.'])
put(5,'I062/380 Final State Selected Altitude',[
 'Two octets: bit 16 MV (Manage Vertical Mode), bit 15 AH (Altitude Hold), bit 14 AM (Approach Mode). For each of MV, AH and AM: 0 = Not active; 1 = Active.',
 'Bits 13 through 1 (Altitude) encode altitude in two\'s complement form. LSB = 25 ft; -1300 ft <= Altitude <= 100000 ft. LNAV status is available from I062/REF/STS/LNAV.'])
put(6,'FAA PHAK Weight Shifting: proportional formulas and worked examples',[
 'For weight shifted within the aircraft: weight shifted / total weight = change in CG / distance weight is shifted. All distances in a calculation use the same unit.',
 'Example 1: weight shifted 100 lb, total weight 8000 lb, shift distance 120 in. Change in CG = 100/8000*120 = 1.5 in. Original CG 77 in aft of datum; new CG = 78.5 in aft of datum.',
 'Example 2: total weight 7800 lb; CG station 81.5 in; aft limit 80.5 in. Move cargo from station 150 to station 30, a forward shift of 120 in. Required CG change = 1.0 in forward. Weight shifted = 7800*1/120 = 65 lb.'], 'worked-formula')
put(7,'FAA PHAK Weight Addition or Removal: worked examples',[
 'Adding weight: added weight / new total weight = CG change / distance between the added weight and old CG. Adding aft of old CG shifts CG aft.',
 'Example: 6860 lb aircraft, CG 80.0 in, add 140 lb at station 150. New weight = 7000 lb; arm difference = 70 in; CG shift = 1.4 in aft; new CG = 81.4 in.',
 'Removing weight: removed weight / new total weight = magnitude of CG change / distance between removed weight and old CG. Removing weight aft of old CG shifts CG forward.',
 'Example: 6100 lb aircraft, CG 80.0 in, remove 100 lb at station 150. New weight = 6000 lb; arm difference = 70 in; CG shift = 100/6000*70 = approximately 1.2 in forward; new CG approximately 78.8 in.'], 'worked-formula')

# Each record is one printed pressure-altitude row: paired ground roll / total
# distance over a 50-ft obstacle, in feet, at 0,10,20,30,40 degrees Celsius.
takeoff={2400:[
 '795/1460 860/1570 925/1685 995/1810 1065/1945',
 '875/1605 940/1725 1015/1860 1090/2000 1170/2155',
 '960/1770 1035/1910 1115/2060 1200/2220 1290/2395',
 '1055/1960 1140/2120 1230/2295 1325/2480 1425/2685',
 '1165/2185 1260/2365 1355/2570 1465/2790 1575/3030',
 '1285/2445 1390/2660 1500/2895 1620/3160 1745/3455',
 '1425/2755 1540/3015 1665/3300 1800/3620 1940/3990',
 '1580/3140 1710/3450 1850/3805 2000/4220 NA/NA',
 '1755/3615 1905/4015 2060/4480 NA/NA NA/NA'],
 2200:[
 '650/1195 700/1280 750/1375 805/1470 865/1575',
 '710/1310 765/1405 825/1510 885/1615 950/1735',
 '780/1440 840/1545 905/1660 975/1785 1045/1915',
 '855/1585 925/1705 995/1835 1070/1975 1150/2130',
 '945/1750 1020/1890 1100/2040 1180/2200 1270/2375',
 '1040/1945 1125/2105 1210/2275 1305/2465 1405/2665',
 '1150/2170 1240/2355 1340/2555 1445/2775 1555/3020',
 '1270/2440 1375/2655 1485/2890 1605/3155 1730/3450',
 '1410/2760 1525/3015 1650/3305 1785/3630 1925/4005'],
 2000:[
 '525/970 565/1035 605/1110 650/1185 695/1265',
 '570/1060 615/1135 665/1215 710/1295 765/1385',
 '625/1160 675/1240 725/1330 780/1425 840/1525',
 '690/1270 740/1365 800/1465 860/1570 920/1685',
 '755/1400 815/1500 880/1615 945/1735 1015/1865',
 '830/1545 900/1660 970/1790 1040/1925 1120/2070',
 '920/1710 990/1845 2145/1990 2145/2145 1235/2315',
 '1015/1900 1095/2055 2405/2225 2405/2405 1370/2605',
 '1125/2125 1215/2305 2715/2500 2715/2715 1520/2950']}
# Preserve apparent published misprints exactly, flag them instead of inferring
# replacement numbers. Image review shows duplicated total distances at 30 C,
# and suspicious 20 C ground-roll values at 6000/7000/8000 ft.
units=['Figure 11-24, SHORT FIELD TAKEOFF DISTANCE. Conditions: maximum weight 2400 lb; flaps 10 degrees; full throttle prior to brake release; paved level runway; zero wind. These are handbook example data, not aircraft-specific operating approval.',
 'Notes: above 3000 ft field elevation, lean mixture for maximum rpm in full-throttle static runup. Decrease distances 10% per 9 knots headwind; tailwind up to 10 knots: increase distances 10% per 2 knots. Dry grass runway: increase distances by 15% of the ground-roll figure. Unavailable printed dashes mean no value; do not interpolate across missing cells.']
for weight,rr in takeoff.items():
 for altitude,values in enumerate(rr):
  for temp,pair in zip([0,10,20,30,40],values.split(),strict=True):
   ground,total=pair.split('/')
   anomaly=weight==2000 and altitude>=6 and temp in [20,30]
   units.append(f'Figure 11-24; weight={weight} lb; pressure altitude={altitude*1000} ft; temperature={temp} C; ground roll={ground} ft; total distance over 50-ft obstacle={total} ft; '+
                ('SOURCE ANOMALY: printed ground-roll value is suspicious; retained as printed, not a validated operational value.' if anomaly else 'printed handbook example.'))
put(8,'FAA PHAK Figure 11-24 Short-field Takeoff Distance',units,notes=['Six suspicious printed ground-roll cells retained with explicit source-anomaly status; no guessed correction.'])
climb={4000:[(605,0,0,0),(570,7,14,13),(530,14,28,27),(485,22,44,43),(430,31,62,63),(365,41,82,87)],
       3700:[(700,0,0,0),(665,6,12,11),(625,12,24,23),(580,19,37,37),(525,26,52,53),(460,34,68,72)],
       3400:[(810,0,0,0),(775,5,10,9),(735,10,21,20),(690,16,32,31),(635,22,44,45),(565,29,57,61)]}
units=['Figure 11-26 NORMAL CLIMB 110 KIAS. Conditions: flaps up; gear up; 2500 rpm; 30 in Hg; fuel flow 120 PPH; cowl flaps open; standard temperature. Time, fuel and distance are cumulative FROM SEA LEVEL, not per-leg quantities.',
 'Add 16 lb fuel for engine start, taxi and takeoff. Increase time, fuel and distance by 10% per 7 C above standard temperature. Distances assume zero wind.']
for weight,rr in climb.items():
 for alt,(rate,t,f,d) in zip([0,4000,8000,12000,16000,20000],rr,strict=True):
  units.append(f'Weight {weight} lb; pressure altitude {alt} ft; rate of climb {rate} fpm; cumulative from sea level: time {t} min, fuel {f} lb, distance {d} NM.')
put(9,'FAA PHAK Figure 11-26 Fuel Time Distance Climb',units)
put(10,'FAA PHAK Figure 9-10: example FAA Form 8130-7 Special Airworthiness Certificate',[
 'This is a reproduced example form, not a current certificate for any project aircraft. Fields A-E: A category/designation and purpose; B manufacturer name and address; C flight from/to; D registration, serial number, builder and model; E issue/expiry dates, operating limitations, FAA representative signature and office/designation.',
 'Printed example: category Special Flight Permit; purpose Production Flight Testing or Customer Demonstration; manufacturer The Boeing Company; address P.O. Box 767, Renton WA 13567 (as printed); flight FROM/TO N/A; registration N-, serial no., builder and model N/A; issue and expiry 01/31/2001; operating limitations dated 01/31/2001; representative Sam T. Smith; office NM-XX. Form shown: FAA Form 8130-7 (07/04).'], 'example-form')

# Acronym columns and TOC leader dots are native text with visual confirmation;
# retain textual ordering, never reinterpret printed page numbers as values.
for i in [11,12,13,14]:
 lines=[re.sub(r'\s+',' ',s).strip() for s in sources[i]['native_exact_text'].splitlines() if s.strip()]
 entries=[]
 for line in lines:
  if '—' in line:entries.append(line)
  elif entries and len(line)>2:entries[-1]+=' '+line
 put(i,'FAA PHAK Acronyms and NOTAM contractions; PDF page '+str(sources[i]['page_numbers'][0]),entries,'glossary')
for i in range(15,26):
 text=' '.join(sources[i]['native_exact_text'].split())
 entries=[];pending=''
 for line in sources[i]['native_exact_text'].splitlines():
  line=line.strip()
  if not line or line=='Page':continue
  pending+=' '+line
  m=re.match(r'^(.*?)\s*\.(?:\s*\.)+\s*(\d+)\s*$',pending.strip())
  if m:
   title=re.sub(r'(?:\.\s*)+$','',m[1]).strip()
   entries.append(f'Contents entry: {title}; printed book page {m[2]}. This is a navigation reference, not a measurement value.')
   pending=''
 assert len(entries)>15,(i,len(entries),pending)
 assert not pending.strip(),(i,pending)
 put(i,'WMO Volume I 2024 contents; PDF page '+str(sources[i]['page_numbers'][0]),entries,'contents-index')

wind=[('Calm','<1','0-0.2','<1','<1'),('Light air','1-3','0.3-1.5','1-5','1-3'),('Light breeze','4-6','1.6-3.3','6-11','4-7'),('Gentle breeze','7-10','3.4-5.4','12-19','8-12'),('Moderate breeze','11-16','5.5-7.9','20-28','13-18'),('Fresh breeze','17-21','8.0-10.7','29-38','19-24'),('Strong breeze','22-27','10.8-13.8','39-49','25-31'),('Near gale','28-33','13.9-17.1','50-61','32-38'),('Gale','34-40','17.2-20.7','62-74','39-46'),('Strong gale','41-47','20.8-24.4','75-88','47-54'),('Storm','48-55','24.5-28.4','89-102','55-63'),('Violent storm','56-63','28.5-32.6','103-117','64-72'),('Hurricane','>=64','>=32.7','>=118','>=73')]
units=['Table 5.1 wind speed equivalents: standard height 10 m above open flat ground. Each speed range below retains its own unit.']
for i,(name,kt,ms,kmh,mph) in enumerate(wind):units.append(f'Beaufort {i}, {name}: {kt} kt; {ms} m/s; {kmh} km/h; {mph} mi/h.')
descriptions=['Calm; smoke rises vertically','Direction shown by smoke-drift, not wind vanes','Wind felt on face; leaves rustle; ordinary vanes moved','Leaves and small twigs in constant motion; wind extends light flag','Raises dust and loose paper; small branches moved','Small trees in leaf begin to sway; crested wavelets on inland waters','Large branches in motion; whistling in telegraph wires; umbrellas used with difficulty','Whole trees in motion; inconvenience walking against wind','Breaks twigs off trees; generally impedes progress','Slight structural damage: chimney-pots and slates removed','Seldom inland; trees uprooted; considerable structural damage','Very rarely experienced; accompanied by widespread damage','No land-description text supplied in this table row']
for i,description in enumerate(descriptions):units[i+1]+=' Land observation: '+description+'. Standard exposure: 10 m above open flat ground.'
put(26,'WMO Table 5.1 Wind speed equivalents',units)
solar=[('Response time, 95% response','<10 s','<15 s'),('Zero offset A, ambient temperature change 5 K/h','1 W/m2','3 W/m2'),('Zero offset B, complete zero offset including A','2 W/m2','4 W/m2'),('Resolution, smallest detectable change','0.5 W/m2','1 W/m2'),('Stability, responsivity change per year','0.5%','1%'),('Temperature response, -10 C to 40 C relative to 20 C','0.5%','1%'),('Non-linearity, irradiance 100-1000 W/m2 relative to responsivity at 500 W/m2','0.2%','0.5%'),('Spectral sensitivity, mean over 300-3000 nm','0.5%','1.0%'),('Clear-sky direct normal incidence spectral error, ISO 9060 clear-sky spectra','0.2%','1%'),('Tilt response, 0 to 90 degrees at 1000 W/m2','0.2%','0.5%'),('Achievable uncertainty, 95% confidence, 1-minute totals','0.9%; 0.56 kJ/m2','1.8%; 1 kJ/m2'),('Achievable uncertainty, 95% confidence, 1-hour totals','0.7%; 21 kJ/m2','1.5%; 54 kJ/m2'),('Achievable uncertainty, 95% confidence, daily totals','0.5%; 200 kJ/m2','1.0%; 400 kJ/m2')]
put(27,'WMO Table 7.2 Characteristics of operational pyrheliometers',[
 f'Table 7.2; {name}: High quality (a) = {high}; Good quality (b) = {good}.' for name,high,good in solar
]+['Footnote a: near state of the art; suitable as a working standard; maintainable only at stations with special facilities and staff. Footnote b: acceptable for network operations. Percentage and absolute uncertainty rows must not be interchanged.'])
sondes=[('Rod thermistor, white paint, MRZ (Russian Federation)',['0.7±0.5','0.5±1','-0.7±1.3'],['1','1.2','1.5'],['1.2-2.2','1.2-2.7','1.5-3.5']),
 ('Copper-constantan thermocouple, Meteolabor (Switzerland)',['a:-0.2; b:-0.05','a:-0.5; b:-0.2','a:-0.8; b:0'],['0.4','0.4','0.8'],['0.6','0.9','1.5']),
 ('Wire thermocapacitor, Vaisala (Finland)',['0±0.2','-0.2±0.2','-0.3±0.3'],['0.4','0.4','0.4'],['0.4-0.7','0.4-0.9','0.4-0.9']),
 ('Chip thermistor, Lockheed Martin Sippican (USA)',['-0.1±0.2','0.2±0.2','0.3±0.3'],['0.3','0.3','0.4'],['0.3-0.6','0.3-0.8','0.4-1.0']),
 ('Bead thermistor (c), aluminized',['0.1±0.2','0±0.3','0±0.5'],['0.4-0.8','0.4-1.3','0.4-1.7'],['0.5-1.0','0.8-1.6','0.4-2.3']),
 ('Multi-thermistor',['±0.2','±0.2','±0.3'],['0.3','0.3','0.4'],['0.3-0.5','0.4-0.6','0.4-0.7'])]
units=['Table 12.5: selected temperature sensors in daytime, WMO international radiosonde comparisons and associated tests, with operational monitoring as in Elms (2003). Columns are grouped by error type, each at pressures 100, 30 and 10 hPa. Values are temperature errors in K; uncertainty uses k=2.']
for sensor,systematic,sonde,uncertainty in sondes:
 for p,s,e,u in zip([100,30,10],systematic,sonde,uncertainty,strict=True):units.append(f'Sensor {sensor}; pressure {p} hPa; systematic error {s} K; sonde error {e} K; uncertainty (k=2) {u} K.')
units.append('Footnote a: as used in Nash et al. (2011). Footnote b: as revised in subsequent tests (Philipona et al., 2013). Footnote c: summary of the range of results from other radiosonde systems using bead thermistors in the Yangjiang comparison (Nash et al., 2011); see Nash (2015) for details of individual types at Yangjiang.')
put(28,'WMO Table 12.5 Daytime radiosonde temperature sensor errors and uncertainty',units)
assert len(authored)==29
result=[]
for i,src in enumerate(sources):
 row={**src,**authored[i],'review':'assistant visual inspection of original region plus native PDF text; not independent human review',
      'image_sha256':hashlib.sha256((OUT/src['image']).read_bytes()).hexdigest()}
 row['common_context']=''
 if i in [8,9]:row['common_context']='\n'.join(row['units'][:2])
 elif i==26:row['common_context']=row['units'][0]
 elif i in [27,28]:row['common_context']=row['units'][-1]+(' '+row['units'][0] if i==28 else '')
 if i==28:row['common_context']+=' WMO comparisons used radiosondes suspended at least 30 m, most commonly 40 m below the balloon. Shorter suspensions can introduce additional daytime bias and sonde error, especially below 30 hPa.'
 elif 15<=i<=25:row['common_context']='WMO Volume I 2024 contents navigation only. Page numbers below are printed book pages, not PDF page indices or measurement values.'
 result.append(row)
(OUT/'reviewed-rewrites.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps({'reviewed_regions':len(result),'units':sum(len(r['units']) for r in result)}))

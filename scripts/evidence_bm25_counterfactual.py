"""Correct DF in an isolated experimental index, never in production source."""
import collections,math
def correct_document_frequency(index):
 df=collections.Counter()
 for frequencies in index._term_frequencies:df.update(frequencies.keys())
 count=len(index.chunks)
 previous=index._idf
 index._idf={term:math.log(1.0+(count-frequency+.5)/(frequency+.5)) for term,frequency in df.items()}
 assert all(0<frequency<=count for frequency in df.values()) and all(value>0 for value in index._idf.values())
 return {'documents':count,'negative_idf_before':sum(v<0 for v in previous.values()),'negative_idf_after':sum(v<0 for v in index._idf.values()),'terms':len(df)}

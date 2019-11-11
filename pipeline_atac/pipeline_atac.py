######################################################
#                                                    #
#                   Pipeline ATAC                    #
#                                                    #
######################################################

# Pipeline for analysis of ATAC-seq data
#
# Tasks:
# 1) mapping
#    - Bowtie2
#    - Duplicate removal
#    - Insert size filtering
#    - Collect QC metrics
# 2) peakcalling
#    - Macs2 callpeak
#    - Subtract blacklists
#    - Merge replicate peaks
#    - Annotate peaks to genes
#    - QC
# 3) counting
#    - Make consensus peakset of all detected peaks
#    - Count reads over consensus peakset
#    - Normalise counts
# 4) bigwigs
#    - Prepare bigWigs for visualisation
#    - Plot coverage at TSS's
# 5) report
#    - run jupyter notebook reports


# Inputs:
#    - Fastq files (paired or single end)
#    - fastq files should be named as such:
#         sample_condition_treatment_replicate.fastq.[1-2].gz (PE)
#         sample_condition_treatment_replicate.fastq.gz (SE)
#    - and placed in data.dir

# Configuration
#    - Pipeline configuration whould be specified in the pipeline.yml

# Outputs
#    - mapped reads, filtered by insert size
#    - called peaks, merged peaks (by replicates)
#    - read counts, for differential accessibility testing
#    - coverage tracks, for visualisation
#    - QC, mapping, peakcalling, signal:background
#    - reports, data exploration and differential accessibility

######################################################

from ruffus import *

import cgatcore.experiment as E
from cgatcore import pipeline as P
import cgatcore.iotools as iotools
from cgat.BamTools import bamtools

import sys
import os
import sqlite3
import re
import glob
import gzip
import pandas as pd
import numpy as np
from pybedtools import BedTool
import seaborn as sns
from matplotlib import pyplot as plt

# Pipeline configuration
P.get_parameters(
		 ["%s/pipeline.yml" % os.path.splitext(__file__)[0],
		  "../pipeline.yml",
		  "pipeline.yml"],
		 )

PARAMS = P.PARAMS

db = PARAMS['database']['url'].split('./')[1]

def connect():
    '''connect to database.
    This method also attaches to helper databases.
    '''

    dbh = sqlite3.connect(db)

    if not os.path.exists(PARAMS["annotations_database"]):
        raise ValueError(
                     "can't find database '%s'" %
                     PARAMS["annotations_database"])

    statement = '''ATTACH DATABASE '%s' as annotations''' % \
    (PARAMS["annotations_database"])

    cc = dbh.cursor()
    cc.execute(statement)
    cc.close()

    return dbh


#####################################################
####              Helper functions               ####
#####################################################

def isPaired(files):
    '''Check whether input files are single or paired end
       Note: this is dependent on files having correct suffix'''
    
    paired = []

    for fastq in files:
        Fpair = re.findall(".*.fastq.1.gz", fastq)
        paired = paired + Fpair

    if len(paired)==0:
        unpaired = True

    else:
        unpaired = False
    
    return unpaired


def writeGreat(locations,basalup,basaldown,maxext,outfile,half=False):
    ''' write out a bed file of great promoters from input gene locations
         locations is [contig,gstart,gend,strand,gene_id] '''

    # Gene regulatory domain definition: 
    # Each gene is assigned a basal regulatory domain of a 
    # minimum distance upstream and downstream of the TSS 
    # (regardless of other nearby genes). 
    # The gene regulatory domain is extended in both directions 
    # to the nearest gene's basal domain but no more than the 
    # maximum extension in one direction

    genome = {}
    for location in locations:
        chrom, gstart, gend, strand_int, gid = location
        if strand_int == "-": 
            strand = "minus" 
            tss = gend
        else: 
            strand = "plus"
            tss = gstart
        record = [tss,strand,gid]
        if chrom[3:5]=="NT" or chrom[3:]=="M": continue
        if chrom not in genome: 
            genome[chrom] = [ record ]
        else: genome[chrom].append(record)

    #add the ends of the chromosomes
    contigs = gzip.open(PARAMS["annotations_dir"]+"/assembly.dir/contigs.bed.gz","rt")

    
    nmatched = 0
    for contig_entry in contigs:
        contig, start, end = contig_entry.strip().split("\t")
        
        if contig in genome.keys():
            genome[contig].append([int(end),"end","end"])
            nmatched+=1
    if nmatched < 21:
        raise ValueError("writeGreat: not enough chromosome ends registered")

    #sort the arrays
    for key in genome.keys():
        genome[key].sort( key = lambda entry: entry[0] )
        
    #now we can walk over the regions and make the regulatory regions.

    greatBed = []
   
    for contig in genome.keys():

        locs = genome[contig]
        contig_end = locs[-1][0]
        for i in range(0,len(locs)):

            l,strand,gid = locs[i]

            if strand == "end": continue

            #get the positions of the neighbouring basal domains.

            # - upstream
            if i == 0: frontstop = 0
            else:
                pl, pstrand, pgid = locs[i-1]
                if pstrand == "plus": frontstop = pl + basaldown
                else: frontstop = pl + basalup
            # - downstream
            nl, nstrand, ngid = locs[i+1]
            if nstrand == "plus": backstop = nl - basalup
            else: backstop = nl - basaldown

            # define basal domain
            if strand=="plus":
                basalstart = l - basalup
                basalend = min( l + basaldown, contig_end )
            else:
                basalstart = l - basaldown
                basalend = min( l + basalup, contig_end )

            # upstream extension
            if frontstop > basalstart:
                regstart = basalstart
            else:
                if half == True:
                    frontext = min( maxext, (l - frontstop) / 2 )
                else:
                    frontext = min( maxext, l - frontstop )
                regstart = l - frontext

            # downstream extension
            if backstop < basalend:
                regend = basalend
            else:
                if half == True:
                    backext = min( maxext, ( backstop - l ) / 2 )
                else:
                    backext = min( maxext, backstop - l )
                regend = l + backext

            # greatBed.append(["chr"+contig,str(regstart),str(regend),gid])
            greatBed.append([contig,str(regstart),str(regend),gid])
        
    outfh = open(outfile,"w")
    outfh.write("\n".join(["\t".join(x) for x in greatBed])+"\n")
    outfh.close()

    
def getTSS(start,end,strand):
    if strand == 1 or strand == "+": tss = start
    elif strand == -1 or strand == "-": tss = end
    else: raise ValueError("getTSS: stand specification not understood")
    return tss


def fetch(query, dbhandle=None, attach=False):
    '''Fetch all query results and return'''

    cc = dbhandle.cursor()

    if attach:
        db_execute(cc, attach)

    sqlresult = cc.execute(query).fetchall()
    cc.close()
    return sqlresult


def fetch_DataFrame(query,
                    dbhandle=db):
    '''Fetch query results and returns them as a pandas dataframe'''

    dbhandle = sqlite3.connect(dbhandle)

    cc = dbhandle.cursor()
    sqlresult = cc.execute(query).fetchall()
    cc.close()

    # see http://pandas.pydata.org/pandas-docs/dev/generated/
    # pandas.DataFrame.from_records.html#pandas.DataFrame.from_records
    # this method is design to handle sql_records with proper type
    # conversion

    field_names = [d[0] for d in cc.description]
    pandas_DataFrame = pd.DataFrame.from_records(
        sqlresult,
        columns=field_names)
    return pandas_DataFrame


# ---------------------------------------------------

# Configure pipeline global variables

Unpaired = isPaired(glob.glob("data.dir/*fastq*gz"))

# ---------------------------------------------------
# Specific pipeline tasks


#####################################################
####                Mapping                      ####
#####################################################
@follows(connect, mkdir("bowtie2.dir"))
@transform("data.dir/*.fastq.1.gz",
           regex(r"data.dir/(.*).fastq.1.gz"),
           r"bowtie2.dir/\1.genome.bam")
def mapBowtie2_PE(infile, outfile):
    '''Map reads with Bowtie2'''
    to_cluster = True

    #    to_cluster = True cgat-flow documentary states that this statement
    # is necessary for every take to be sumbitted to cluster, however their code doesn't have it
    
    if len(infile) == 0:
        pass

    read1 = infile
    read2 = infile.replace(".1.gz", ".2.gz")

    log = outfile + "_bowtie2.log"
    tmp_dir = "$SCRATCH_DIR"

    options = PARAMS["bowtie2_options"]
    genome = os.path.join(PARAMS["bowtie2_genomedir"], PARAMS["bowtie2_genome"])

    statement = '''tmp=`mktemp -p %(tmp_dir)s` && 
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x %(genome)s
                     -1 %(read1)s 
                     -2 %(read2)s
                     %(options)s
                     1> $tmp 
                     2> %(log)s && 
                   samtools sort -O BAM -o %(outfile)s $tmp && 
                   samtools index %(outfile)s && 
                   rm $tmp''' % locals()

    P.run(statement,job_memory="2G",job_threads=12)


@active_if(Unpaired)
@transform("data.dir/*.fastq.gz",
           regex(r"data.dir/(.*).fastq.gz"),
           r"bowtie2.dir/\1.genome.bam")
def mapBowtie2_SE(infile, outfile):
    '''Map reads with Bowtie2'''

    if len(infile) == 0:
        pass
    
    log = outfile + "_bowtie2.log"
    tmp_dir = "$SCRATCH_DIR"

    options = PARAMS["bowtie2_options"]
    genome = os.path.join(PARAMS["bowtie2_genomedir"], PARAMS["bowtie2_genome"])

    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x %(genome)s
                     -U %(infile)s 
                     %(options)s
                     1> $tmp 
                     2> %(log)s &&
                   samtools sort -O BAM -o %(outfile)s $tmp &&
                   samtools index %(outfile)s &&
                   rm $tmp''' % locals()

    P.run(statement,job_memory="2G",job_threads=12)

    
@follows(mapBowtie2_PE, mapBowtie2_SE)
@transform("bowtie2.dir/*.genome.bam", suffix(r".genome.bam"), r".filt.bam")
def filterBam(infile, outfile):
    '''filter bams on MAPQ >10, & remove reads mapping to chrM before peakcalling'''

    local_tmpdir = "/gfs/scratch/"
        
    statement = '''tmp=`mktemp -p %(local_tmpdir)s` && 
                   head=`mktemp -p %(local_tmpdir)s` &&
                   samtools view -h %(infile)s | grep "^@" - > $head  && 
                   samtools view -q10 %(infile)s | 
                     grep -v "chrM" - | 
                     cat $head - |
                     samtools view -h -o $tmp -  && 
                   samtools sort -O BAM -o %(outfile)s $tmp  &&
                   samtools index %(outfile)s &&
                   rm $tmp $head''' % locals()

    P.run(statement, job_memory="10G", job_threads=2)
    

@transform(filterBam,
           regex(r"(.*).filt.bam"),
           r"\1.prep.bam")
def removeDuplicates(infile, outfile):
    '''PicardTools remove duplicates'''

    metrics_file = outfile + ".picardmetrics"
    log = outfile + ".picardlog"
    tmp_dir = "$SCRATCH_DIR"

    statement = '''tmp=`mktemp -p %(tmp_dir)s` && 
                   MarkDuplicates 
                     INPUT=%(infile)s 
                     ASSUME_SORTED=true 
                     REMOVE_DUPLICATES=true 
                     QUIET=true 
                     OUTPUT=$tmp 
                     METRICS_FILE=%(metrics_file)s 
                     VALIDATION_STRINGENCY=SILENT
                     2> %(log)s  && 
                   mv $tmp %(outfile)s && 
                   samtools index %(outfile)s'''

    P.run(statement, job_memory="12G", job_threads=2)

    
@active_if(Unpaired == False)
@transform(removeDuplicates,
           suffix(r".prep.bam"),
           r".size_filt.prep.bam")
def size_filterBam(infile, outfile):
    '''filter bams on insert size (max size specified in ini)'''

    local_tmpdir = "$SCRATCH_DIR"

    insert_size_filter_F = PARAMS["bowtie2_insert_size"]
    insert_size_filter_R = "-" + str(insert_size_filter_F) # reverse reads have "-" prefix for TLEN

    statement = '''tmp=`mktemp -p %(local_tmpdir)s` && 
                   head=`mktemp -p %(local_tmpdir)s` && 
                   samtools view -h %(infile)s | grep "^@" - > $head  && 
                   samtools view %(infile)s | 
                     awk 'BEGIN {OFS="\\t"} {if ($9 ~ /^-/ && $9 > %(insert_size_filter_R)s) print $0 ;
                       else if ($9 ~ /^[0-9]/ && $9 < %(insert_size_filter_F)s) print $0}' - |     
                     cat $head - |
                     samtools view -h -o $tmp -  && 
                   samtools sort -O BAM -o %(outfile)s $tmp  && 
                   samtools index %(outfile)s &&
                   rm $tmp $head''' % locals()

    P.run(statement, job_memory="10G", job_threads=2)

    
@follows(size_filterBam)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".bam.bai")
def indexBam(infile, outfile):
    '''index bams, if index failed to be generated'''

    statement = '''samtools index -b %(infile)s > %(outfile)s'''

    P.run(statement)

    
####################################################
#####               Mapping QC                 #####
####################################################
@follows(indexBam)
@transform("bowtie2.dir/*.genome.bam",
           regex(r"(.*).genome.bam"),
           r"\1.contigs.counts")
def contigReadCounts(infile, outfile):
    '''count reads mapped to each contig'''

    tmp_dir = "$SCRATCH_DIR"
    name = os.path.basename(infile).rstrip(".bam")
    
    statement =  '''tmp=`mktemp -p %(tmp_dir)s` && 
                    samtools idxstats %(infile)s > $tmp &&
                    awk 'BEGIN {OFS="\\t"} {print $0,"%(name)s"}' $tmp > %(outfile)s &&
                    rm $tmp'''

    P.run(statement)


@follows(contigReadCounts)
@merge("bowtie2.dir/*.contigs.counts", "allContig.counts")
def mergeContigCounts(infiles, outfile):

    infiles = ' '.join(infiles)
    
    statement = '''cat %(infiles)s > %(outfile)s'''

    P.run(statement)

    
@transform(mergeContigCounts, suffix(r".counts"), r".load")
def loadmergeContigCounts(infile, outfile):

    P.load(infile, outfile, options='-H "contig,length,mapped_reads,unmapped_reads,sample_id" ')

    
@follows(loadmergeContigCounts)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".flagstats.txt")
def flagstatBam(infile, outfile):
    '''get samtools flagstats for bams'''

    statement = '''samtools flagstat %(infile)s > %(outfile)s'''
    
    P.run(statement)


@merge(flagstatBam, "bowtie2.dir/flagstats.load")
def loadflagstatBam(infiles, outfile):
    '''Summarize & load samtools flagstats'''
    
    n = 0

    for infile in infiles:

        n = n + 1
        
        QC_passed = []
        QC_failed = []
        cat = ['total', 'secondary', 'supplementary', 'duplicates', 'mapped', 'paired_in_sequencing', 'read1', 'read2',
                'properly_paired', 'itself_and_mate_mapped', 'singletons', 'mate_mapped_2_diff_chr', 'mate_mapped_2_diff_chr_and_MAPQ_5+']

        name = '_'.join(os.path.basename(infile).split(".")[0:2])

        with open(infile, "r") as o:
            for line in o:
                QC_passed.append(line.split(" ")[0])
                QC_failed.append(line.split(" ")[2])

        QCpass = dict(zip(cat, QC_passed))
        QCfail = dict(zip(cat, QC_failed))

        pass_df = pd.DataFrame.from_dict(QCpass, orient="index").transpose()
        pass_df["QC_status"] = "pass"

        fail_df = pd.DataFrame.from_dict(QCfail, orient="index").transpose()
        fail_df["QC_status"] = "fail"

        if n == 1:
            table = pass_df.append(fail_df)
            table["sample_id"] = name
            
        else:
            df = pass_df.append(fail_df)
            df["sample_id"] = name
            table = table.append(df)

    table_txt = outfile.replace(".load", ".txt")
    table.to_csv(table_txt, sep="\t", header=True, index=False)

    P.load(table_txt, outfile)
            

@follows(loadflagstatBam)
@transform("bowtie2.dir/*.bam",
           regex(r"(\S+)\.(.*).bam"),
           r"\1_\2.picardAlignmentStats.txt")
def picardAlignmentSummary(infile, outfile):
    '''get aligntment summary stats with picard'''

    tmp_dir = "$SCRATCH_DIR"
    refSeq = os.path.join(PARAMS["genome_dir"], PARAMS["genome"] + ".fa")
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s` && 
                   java -Xms12G -Xmx14G -jar /gfs/apps/bio/picard-tools-2.15.0/picard.jar CollectAlignmentSummaryMetrics
                     R=%(refSeq)s
                     I=%(infile)s
                     O=$tmp && 
                   cat $tmp | grep -v "#" > %(outfile)s'''

    P.run(statement, job_memory="5G", job_threads=3)

    
@merge(picardAlignmentSummary,
       "picardAlignmentSummary.load")
def loadpicardAlignmentSummary(infiles, outfile):
    '''load the complexity metrics to a single table in the db'''

    P.concatenate_and_load(infiles, outfile,
                         regex_filename=".*/(.*).picardAlignmentStats",
                         cat="sample_id",
                         options='-i "sample_id"')


@active_if(Unpaired == False)
@follows(flagstatBam)
@transform("bowtie2.dir/*.bam",
           regex(r"(\S+)\.(.*).bam"),
           r"\1_\2.picardInsertSizeMetrics.txt")
def picardInsertSizes(infile, outfile):
    '''get aligntment summary stats with picard'''

    tmp_dir = "$SCRATCH_DIR"

    pdf = outfile.replace("Metrics.txt", "Histogram.pdf")
    histogram = outfile.replace("Metrics.txt", "Histogram.txt")
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s` && 
                   java -Xms12G -Xmx32G -jar /gfs/apps/bio/picard-tools-2.15.0/picard.jar CollectInsertSizeMetrics
                     I=%(infile)s 
                     O=$tmp
                     H=%(pdf)s
                     M=0.5 && 
                   cat $tmp | grep -A`wc -l $tmp | tr "[[:blank:]]" "\\n" | head -n 1` "## HISTOGRAM" $tmp | grep -v "#" > %(histogram)s &&
                   cat $tmp | grep -A 2 "## METRICS CLASS" $tmp | grep -v "#" > %(outfile)s &&
                   rm $tmp'''

    P.run(statement, job_memory="8G", job_threads=5)

    
@merge(picardInsertSizes,
       "picardInsertSizeMetrics.load")
def loadpicardInsertSizeMetrics(infiles, outfile):
    '''load the insert size metrics to a single table in the db'''

    P.concatenate_and_load(infiles, outfile,
                         regex_filename=".*/(.*).picardInsertSizeMetrics",
                         cat="sample_id",
                         options='-i "sample_id"')


@follows(picardInsertSizes)
@merge("bowtie2.dir/*.picardInsertSizeHistogram.txt",
       "picardInsertSizeHistogram.load")
def loadpicardInsertSizeHistogram(infiles, outfile):
    '''load the insert size metrics to a single table in the db'''

    P.concatenate_and_load(infiles, outfile,
                         regex_filename=".*/(.*).picardInsertSizeHistogram",
                         cat="sample_id",
                         options='-i "sample_id"')

    
@follows(loadpicardAlignmentSummary, loadpicardInsertSizeMetrics, loadpicardInsertSizeHistogram)
def mapping():
    pass


####################################################
#####              Peakcalling                 #####
####################################################
@active_if(Unpaired)
@follows(mapping, mkdir("macs2.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex("bowtie2.dir/(.*).prep.bam"),
           r"macs2.dir/\1.macs2.fragment_size.tsv")
def macs2Predictd(infile, outfile):
    '''predict fragment sizes for SE ChIP'''

    options = PARAMS["macs2_se_options"]
    outdir = os.path.dirname(outfile)
    
    statement = '''macs2 predictd 
                     --format BAM 
                     --ifile %(infile)s 
                     --outdir %(outdir)s 
                     --verbose 2 %(options)s 
                     2> %(outfile)s'''

    P.run(statement, job_condaenv="macs2", job_threads=4)

    
@active_if(Unpaired)
@transform(macs2Predictd, suffix(r".fragment_size.tsv"), r".fragment_size.txt")
def getFragmentSize(infile, outfile):
    '''Get fragment sizes from macs2 predictd'''

    sample = os.path.basename(infile).rstrip(".fragment_size.tsv")
    tmp_dir = "$SCRATCH_DIR"
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                   echo %(sample)s `cat %(infile)s | 
                     grep "# tag size =" | 
                     tr -s " " "\\t" | 
                     awk 'BEGIN {OFS="\\t "} {print $12}'` > $tmp && 
                   cat $tmp | tr -s " " "\\t" > %(outfile)s &&
                   rm $tmp'''

    P.run(statement)

    
@active_if(Unpaired)
@transform(getFragmentSize, suffix(r".txt"), r".load")
def loadgetFragmentSize(infile, outfile):
    P.load(infile, outfile, options='-H "sample, tag_size"')


@follows(loadgetFragmentSize)
@transform("bowtie2.dir/*.prep.bam",
           regex("bowtie2.dir/(.*).prep.bam"),
           r"macs2.dir/\1.macs2.log")
def macs2callpeaks(infile, outfile):
    '''call peaks with macs2'''
    
    if bamtools.is_paired(infile):

        options = PARAMS["macs2_pe_options"]
        name = os.path.basename(outfile).replace(".macs2.log", "")
        
        statement='''macs2 callpeak 
                       --outdir macs2.dir                  
                       --bdg
                       --SPMR
                       %(options)s 
                       --treatment %(infile)s 
                       --name %(name)s 
                       >& %(outfile)s'''  

    else:
        # get macs2 predictd fragment lengths from csvdb
        table = os.path.basename(infile).replace(".fastq.1.gz", ".macs2.fragment_size").replace("-", "_").replace(".", "_")

        query = '''select tag_size from %(table)s ''' % locals()

        dbh = sqlite3.connect(db)
        cc = dbh.cursor()
        sqlresult = cc.execute(query).fetchall()

        fragment_length = sqlresult[0]
        fragment_length = fragment_length[0]

        # run macs2 callpeak
        options = PARAMS["macs2_se_options"]
        name = os.path.basename(outfile).split(".")[0]
        tmp_dir = "$SCRATCH_DIR"

        statement='''macs2 callpeak 
                       --outdir macs2.dir 
                       --bdg
                       --SPMR
                       %(options)s 
                       --treatment %(infile)s 
                       --name %(name)s 
                       >& %(outfile)s'''  
    
    P.run(statement, job_condaenv="macs2", job_threads=5)

    
@follows(macs2callpeaks)
@files(None, "blacklist_chip.mm10.bed.gz")
def getChIPblacklist(infile, outfile):
    '''Get Ensembl ChIP blacklisted regions'''

    chip_blacklist = PARAMS["peak_filter_chip_blacklist"]
    statement = '''wget -O %(outfile)s %(chip_blacklist)s'''

    P.run(statement)

    
@follows(getChIPblacklist)
@files(None, "blacklist_atac.mm10.bed.gz")
def getATACblacklist(infile, outfile):
    '''Get ATAC blacklist regions'''

    atac_blacklist = PARAMS["peak_filter_atac_blacklist"]
    statement = '''wget -q %(atac_blacklist)s | gzip - > %(outfile)s'''

    P.run(statement)

    
@follows(getATACblacklist)
@transform("macs2.dir/*.narrowPeak",
           regex("(.*).narrowPeak"),
           add_inputs([getChIPblacklist, getATACblacklist]),
           r"\1.filt.bed")
def filterPeaks(infiles, outfile):
    '''subtract blacklist regions from peakset'''

    peak, blacklists = infiles

    blacklist = ' '.join(blacklists)
    
    statement = '''intersectBed -wa -v -a %(peak)s -b <(zcat %(blacklist)s ) > %(outfile)s'''
    
    P.run(statement)


def mergeReplicatePeaksGenerator():
    '''Get replicate info from pipeline.ini & create jobs'''

    if PARAMS["replicates_auto_merge"]:
        peaksets = [".all", ".size_filt"]

        for peaks in peaksets:

            pfiles = glob.glob("macs2.dir/*.filt.bed")
            
            if len(pfiles)==0:
                pass

            if peaks ==".all":
                pfiles = [x for x in pfiles if "size_filt" not in x]
            else:
                pfiles = [x for x in pfiles if "size_filt" in x]

            reps = {}
            for p in pfiles:
                m = re.match(r"macs2.dir/(.*)_([1-9]).*.filt.bed", p) 
                sample = m.group(1)
                rfile = m.group(0)

                if sample in reps:
                    reps[sample].append(rfile)
                else:
                    reps[sample] = [rfile]

            for key in reps:
                yield [reps[key], "macs2.dir/" + key + peaks + ".merged.bed" ]

    else:
        replicates = PARAMS["replicates_pairs"]

        outDir = "macs2.dir/"

        peaksets = [".all", ".size_filt"]

        for peaks in peaksets:
            if peaks == ".all":
                suffix = "_peaks.filt.bed"
            if peaks == ".size_filt":
                suffix = ".size_filt_peaks.filt.bed"

            for reps in replicates.split('\n'):

                reps = reps.split(",")

                if len(reps)==3:
                    out = outDir + reps[2] + peaks + ".merged.bed"
                    bed1 = outDir + reps[0] + suffix
                    bed2 = outDir + reps[1] + suffix

                    yield [ [bed1, bed2], out]

                if len(reps)>3:
                    out = outDir + reps[-1] + peaks + ".merged.bed"
                    bed1 = outDir + reps[0] + suffix
                    bed2 = outDir + reps[1] + suffix
                    bed3 = outDir + reps[2] + suffix

                    yield [ [bed1, bed2, bed3], out]

            
@follows(filterPeaks)
@files(mergeReplicatePeaksGenerator)
def mergeReplicatePeaks(infiles, outfile):
    '''Merge replicate peaks'''

    tmp = outfile.replace(".bed", ".tmp")

    rep_overlaps = PARAMS["replicates_overlap"] # set this as option in pipeline.ini
    
    if len(infiles)==2:
        beda = BedTool(infiles[0])
        bedb = BedTool(infiles[1])
        
        # cat peaks from all reps
        bedab = beda.cat(bedb, postmerge=False)

        # merge overlapping peaks, perform summary operations on additional cols
        bedab = bedab.sort().merge(c="4,5,6,7,8,9,10", o="collapse,mean,first,mean,min,min,mean")
        bedab.saveas(tmp) # save bed

    if len(infiles)==3:
        beda = BedTool(infiles[0])
        bedb = BedTool(infiles[1])
        bedc = BedTool(infiles[2])

        bedab = beda.cat(bedb, postmerge=False)
        bedabc = bedab.cat(bedc, postmerge=False)

        bedabc = bedabc.sort().merge(c="4,5,6,7,8,9,10", o="collapse,mean,first,mean,min,min,mean")
        bedabc.saveas(tmp)

    with open(outfile, "w") as output:
        with open(tmp, "r") as r:
            for line in r:
                cols = [x.rstrip("\n") for x in line.split("\t")]

                [contig, start, end, peak_id, peak_score, strand, FC, pval, qval, summit] = cols

                reps = [x.split("_")[3] for x in peak_id.split(",")]

                # get unique elements from list of replicates by converting to set
                if len(set(reps)) >= rep_overlaps:
                    # outfile
                    bed = [contig, start, end, peak_id, peak_score, strand, FC, pval, qval, summit]
                    bed = '\t'.join(bed) + '\n'

                    output.write(bed)

    statement = '''rm %(tmp)s'''

    P.run(statement)


@follows(mergeReplicatePeaks)
@files(None, "macs2.dir/no_peaks.txt")
def countPeaks(infiles, outfile):

    beds = glob.glob("./macs2.dir/*filt.bed")
    merge_beds = glob.glob("./macs2.dir/*merged.bed")

    peaksets = [beds, merge_beds]

    if len(peaksets)==0:
        pass
    
    no_peaks = {}

    for peaks in peaksets:
        for bed in peaks:
            if "merged" in bed:
                name = '.'.join(os.path.basename(bed).split(".")[0:3]).replace(".all", "")
            else:
                name = os.path.basename(bed).replace("_peaks.filt.bed", "")

            df = pd.read_csv(bed, sep="\t", header=None)

            no_peaks[name] = len(df)

    peaks = pd.DataFrame.from_dict(no_peaks, orient="index")

    peaks["sample_id"] = peaks.index.values
    peaks["size_filt"] = peaks["sample_id"].apply(lambda x: "all_fragments" if "size_filt" not in x else "<150bp")
    peaks["merged"] = peaks.apply(lambda x: "merged" if "merged" in x.sample_id else "replicate", axis=1)
    peaks["sample_id"] = peaks["sample_id"].apply(lambda x: x.split(".")[0].rstrip("_merged"))
    peaks = peaks.rename(columns={0:"no_peaks"})
    peaks.reset_index(inplace=True, drop=True)

    peaks.to_csv(outfile, header=True, index=False, sep="\t")

    
@transform(countPeaks, suffix(".txt"), ".load")
def loadcountPeaks(infile, outfile):
    P.load(infile, outfile, options='-i "sample_id" ')

    
@follows(loadcountPeaks)
def peakcalling():
    pass

#######################################################

############
## HMMRATAC - test
############
@follows(mkdir("hmmratac.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex("bowtie2.dir/(.*).prep.bam"),
           r"hmmratac.dir/\1.hmmratac.log")
def hmmratac(infile, outfile):

    
    index = infile.replace(".bam", ".bam.bai")
    contigs = PARAMS["annotations_chrom_sizes"]
    exe = PARAMS["hmmr_executable"]
    options = PARAMS["hmmr_options"]
    name = outfile.replace(".hmmratac.log", "")
    
    blacklists = ' '.join(["blacklist_chip.mm10.bed.gz", "blacklist_chip.mm10.bed.gz"])
    
    statement = '''blacklist=`mktemp -p $SCRATCH_DIR` &&
                   zcat %(blacklists)s > $blacklist &&
                   java -Xms20G -Xmx40G -jar %(exe)s 
                     -b %(infile)s
                     -i %(index)s
                     -g %(contigs)s
                     -o %(name)s
                     %(options)s
                     --blacklist $blacklist &&
                   rm $blacklist'''

    # HMMRAATAC chooses training regions by selecting areas where the fold change
    # (above genomic background) is between 10-20 (by default, set by -l & -u parameters)
    # this setting seems to be far too low >1x10^6 "peaks" called in first test.
    # I will alter these parameters (increase both) to increase the stringency of peak detection

    # a measurement of enrichment over genomic background for macs2 detected peaks would be a good
    # metric for tailoring these parameters

    ########################################
    #### linear enrichment over background

    ## 1st get macs2 linear fold enrichment vs background model
    # macs2 bdgcmp -t L1_WT_D5_0hr_r1.size_filt_treat_pileup.bdg -c L1_WT_D5_0hr_r1.size_filt_control_lambda.bdg -m FE -o L1_WT_D5_0hr_r1.size_filt.FE.bdg

    ## now compare average signal at macs2 called peaks and outside them

    # intersectBed -v -b L1_WT_D5_0hr_r1.size_filt_peaks.filt.bed -a L1_WT_D5_0hr_r1.size_filt.FE.bdg  | awk 'BEGIN {OFS="\t"} {sum+=$4} END {print sum/NR}'
    ## Average genomic signal = 2.84921
    
    # intersectBed -b L1_WT_D5_0hr_r1.size_filt_peaks.filt.bed -a L1_WT_D5_0hr_r1.size_filt.FE.bdg  | awk 'BEGIN {OFS="\t"} {sum+=$4} END {print sum/NR}'
    ## Average peak signal = 17.18

    ### so defualts for HMMRATAC of -l 10 and -u 20 seem appropriate...
    # however are still far too sensitive!

    
    ##### Repeat with non-size selected signal & peaks!
    # macs2 bdgcmp -t L1_WT_D5_0hr_r1_treat_pileup.bdg -c L1_WT_D5_0hr_r1_control_lambda.bdg -m FE -o L1_WT_D5_0hr_r1.FE.bdg

    # intersectBed -b L1_WT_D5_0hr_r1_peaks.filt.bed -a L1_WT_D5_0hr_r1.FE.bdg  | awk 'BEGIN {OFS="\t"} {s+=$4} END {print s/NR}' -
    # average signal (all fragments) at non-size selected peaks = 6.38166

    # intersectBed  -b L1_WT_D5_0hr_r1.size_filt_peaks.filt.bed -a L1_WT_D5_0hr_r1.FE.bdg  | awk 'BEGIN {OFS="\t"} {s+=$4} END {print s/NR}' -
    # average signal (all fragments) at size selected peaks = 7.18366

    # intersectBed -v -b L1_WT_D5_0hr_r1.size_filt_peaks.filt.bed -a L1_WT_D5_0hr_r1.FE.bdg  | awk 'BEGIN {OFS="\t"} {s+=$4} END {print s/NR}' -
    # average background signal (all fragments) = 1.13039

    ## so if HMMRATAC signal:background ratios for selecting training sites use total signal (from all fragment sizes) only areas of strong enrichment
    ## should be selected... 
    print(statement)

    P.run(statement, job_memory="40G")
    



#######################################################


    
########################################################
####                    FRIP                        ####
########################################################
def generate_FRIPcountBAM_jobs():

    all_intervals = glob.glob("macs2.dir/*_peaks.narrowPeak")
    all_bams = glob.glob("bowtie2.dir/*.prep.bam")
        
    outDir = "FRIP.dir/"

    # group size filtered and non-size filtered files seperately
    bams = [x for x in all_bams if "size_filt" in x]
    intervals = [x for x in all_intervals if "size_filt" in x]
    size_filt = [bams, intervals]
    
    bams = [x for x in all_bams if "size_filt" not in x]
    intervals = [x for x in all_intervals if "size_filt" not in x]
    non_size_filt = [intervals, bams]

    # iterate over grouped files matching bams & peaks
    for group in [size_filt, non_size_filt]:
        group = sum(group, []) # first flatten list

        intervals = [x for x in group if "narrowPeak" in x]
        bams = [x for x in group if "prep.bam" in x]

        for interval in intervals:
            ifile = [i.split("/")[-1].rstrip("_peaks.narrowPeak") for i in [interval] ]
            match = ''.join(ifile)

            for bam in bams:
                bam_sample = bam.split("/")[-1].rstrip(".prep.bam")

                if match in bam and match in interval:
                    bfile = [b.split("/")[-1][:-len(".prep.bam")] for b in [bam] ]

                    bedfile = ' '.join(str(x) for x in ifile )
                    bamfile = ' '.join(str(x) for x in bfile )

                    output = outDir + bedfile + "_fripcounts" + ".txt"

                    a = os.path.basename(interval)[:-len("_peaks.narrowPeak")]
                    b = os.path.basename(bam)[:-len(".prep.bam")]
                    c = os.path.basename(output)[:-len("_fripcounts.txt")]

                    if a == b and b == c: #sanity check
                        yield ( [ [interval, bam], output ] )

                        
@follows(mkdir("FRIP.dir"), peakcalling)
@files(generate_FRIPcountBAM_jobs)
def FRIPcountBAM(infiles, outfile):
    '''use bedtools to count reads in bed intervals'''

    interval, bam = infiles

    if bamtools.is_paired(bam):
         # -p flag specifes only to count paired reads

        statement = '''bedtools multicov -p -q 10 -bams %(bam)s 
                    -bed <( awk 'BEGIN {OFS="\\t"} {print $1,$2,$3,$4,$5,$3-$2}' %(interval)s ) 
                    > %(outfile)s 
                    && sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\ttotal' 
                    %(outfile)s''' % locals()

    else:

         statement = '''bedtools multicov -q 10 -bams %(bam)s 
                     -bed <( awk 'BEGIN {OFS="\\t"} {print $1,$2,$3,$4,$5,$3-$2}' %(interval)s ) 
                     > %(outfile)s 
                     && sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\ttotal' 
                     %(outfile)s''' % locals()

    print(statement)
         
    P.run(statement)

    
@transform(FRIPcountBAM,
           regex(r"(.*)_fripcounts.txt"),
           r"\1_frip.txt")
def FRIP(infile, outfile):
        '''Calculate fraction of read in peaks'''

        if "size_filt" in infile:
            size_filt = '''"<150bp"'''
        else:
            size_filt = "all_fragments"

        sample_name = os.path.basename(infile).replace("_fripcounts.txt", "")
        sample_label = sample_name.split(".")[0]
        bam = "bowtie2.dir/" + sample_name + ".prep.bam"
        

        statement = '''total_reads=`samtools view %(bam)s | 
                         wc -l` &&
                       peak_reads=`awk 'BEGIN {OFS="\\t"} 
                         {sum += $7;} END 
                         {print sum;}' %(infile)s` &&
                       awk -v sample=%(sample_label)s 
                         -v isize=%(size_filt)s
                         -v t_reads=$total_reads
                         -v p_reads=$peak_reads
                         'BEGIN {print p_reads / t_reads, sample, isize}' | 
                         tr -s "[[:blank:]]" "\\t" 
                         > %(outfile)s'''

        print(statement)

        P.run(statement)

@merge(FRIP, "FRIP.dir/frip_table.txt")
def FRIP_table(infiles, outfile):
    '''merge all files to load to csvdb'''    

    infiles = ' '.join(infiles)

    tmp_dir = "$SCRATCH_DIR"
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                   awk 'BEGIN {printf "FRIP\\tsample_id\\tsize_filt\\n"}' > $tmp &&
                   for i in %(infiles)s; 
                     do cat $i >> $tmp; done;
                   mv $tmp %(outfile)s'''

    P.run(statement)


@transform(FRIP_table, suffix(".txt"), ".load")
def loadFRIP_table(infile, outfile):
    P.load(infile, outfile, options = '-i "sample_id"')


@follows(loadFRIP_table)
def frip():
    pass

    
########################################################
####                  Merge Peaks                   ####
########################################################
@follows(mkdir("BAM_counts.dir"), frip)
@merge("macs2.dir/*_peaks.filt.bed", "BAM_counts.dir/merged_peaks.bed")
def mergePeaks(infiles, outfile):
    '''cat all peak files, center over peak summit +/- 250 b.p., then merge peaks'''

    tmp_dir = "$SCRATCH_DIR"
    window_size = PARAMS["read_counts_window"]
    offset = int(window_size)/2

    # defualt is to use non size filtered peaks
    if PARAMS["macs2_peaks"] == "all":
        infiles = [x for x in infiles if "size_filt" not in x]
    if PARAMS["macs2_peaks"] == "size_filt":
        infiles = [x for x in infiles if "size_filt" in x]

    infiles = ' '.join(infiles)

    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                   cat %(infiles)s | grep -v ^chrUn* - > $tmp &&
                   awk 'BEGIN {OFS="\\t"} {center = $2 + $10 ; start = center - %(offset)s ; end = center + %(offset)s ;
                     print $1,start,end,$4,$5}' $tmp | 
                   awk 'BEGIN {OFS="\\t"} {if (($2 < $3) && ($2 > 0)) print $0}' - |
                   sort -k1,1 -k2,2n |
                   mergeBed -c 4,5 -o count,mean -i - |
                   awk 'BEGIN {OFS="\\t"} {print $1,$2,$3,"merged_peaks_"NR,$5,$4,$3-$2,sprintf("%%i", ($2+$3)/2)}' - > %(outfile)s &&
                   rm $tmp'''

    P.run(statement)

    
########################################################
####                GREAT Peak2Gene                 ####
########################################################
@follows(mergePeaks, mkdir("annotations.dir"))
@files(None,"annotations.dir/ensemblGeneset.txt")
def fetchEnsemblGeneset(infile,outfile):
    ''' Get the *latest* gene records using biomart. The aim here is NOT to match
        the great gene set: For that we would only want protein coding genes with
        GO annotations '''

    statement = '''select gi.gene_id, gi.gene_name,
                          gs.contig, gs.start, gs.end, gs.strand
                   from gene_info gi
                   inner join gene_stats gs
                   on gi.gene_id=gs.gene_id
                   where gi.gene_biotype="protein_coding"
                '''

    anndb = os.path.join(PARAMS["annotations_dir"],
                         "csvdb")

    df = fetch_DataFrame(statement, anndb)
    df.to_csv(outfile, index=False, sep="\t", header=True)

    
@transform(fetchEnsemblGeneset,suffix(".txt"),".load")
def uploadEnsGenes(infile,outfile):
    '''Load the ensembl annotation including placeholder GO ID's'''
    P.load(infile, outfile, options='-i "gene_id" -i "go_id" ')

    
@follows(uploadEnsGenes)
def getGeneLists():
    pass


@follows(getGeneLists, mkdir("greatBeds.dir"))
@files(uploadEnsGenes, "greatBeds.dir/ens_great_prom.bed")
def greatPromoters(infile,outfile):
    ''' Make great promoters for the genes retrieved from Ensembl'''

    dbh = sqlite3.connect(db)
    cc = dbh.cursor()

    basalup = PARAMS["great_basal_up"]
    basaldown = PARAMS["great_basal_down"]
    maxext = PARAMS["great_max"]
    half = PARAMS["great_half"]  
    statement = '''select distinct contig, start,                                                                                                         
                   end, strand, gene_id from ensemblGeneset '''

    result = cc.execute(statement).fetchall()
    
    
    locations = [ [ str(r[0]), int(r[1]), int(r[2]),str(r[3]), str(r[4]) ] 
                   for r in result ]
    
    writeGreat(locations,basalup,basaldown,maxext,outfile,half)


@transform(greatPromoters,
           regex(r"(.*)_prom.bed"),
           r"\1.bed")
def filterEnsPromoters(infile,outfile):
    '''Remove unwanted chromosomes & correct contig column, "chrchr" -> "chr"'''

    tmp_dir = "$SCRATCH_DIR"
    statement = '''tmp=`mktemp -p %(tmp_dir)s` && 
                sed 's/chrchr/chr/' %(infile)s > $tmp &&
                grep -v ^chrM $tmp > %(outfile)s && rm $tmp'''

    P.run(statement)

    
@transform(filterEnsPromoters,suffix(".bed"),".load")
def loadGreatPromoters(infile, outfile):
    '''Load the great promoter regions'''
    P.load(infile, outfile, options='-H "chr,start,end,gene_id" -i "gene_id"')

    
@follows(loadGreatPromoters)
def GreatAnnotation():
    pass


@follows(GreatAnnotation, mkdir("regulated_genes.dir"))
@transform("BAM_counts.dir/merged_peaks.bed",
           regex(r"BAM_counts.dir/(.*).bed"),
           add_inputs("greatBeds.dir/ens_great.bed"),
           r"regulated_genes.dir/\1.GREAT.txt")
def regulatedGenes(infiles,outfile):
    '''Get all genes associated with peaks'''

    infile, greatPromoters = infiles

    # intersect infiles with great gene annotation beds to get peak associated genes
    statement = '''intersectBed -wa -wb -a <(cut -f1-8 %(infile)s) -b %(greatPromoters)s 
                | cut -f1-8,12 > %(outfile)s''' % locals()

    # Filter on nearest peak 2 gene later

    P.run(statement)

    
@transform(regulatedGenes, suffix(r".txt"), r".load")
def loadRegulatedGenes(infile, outfile):
    P.load(infile, outfile, 
           options='-H "contig,start,end,peak_id,peak_score,no_peaks,peak_width,peak_centre,gene_id" -i "peak_id"')

    
@transform(loadRegulatedGenes,
           suffix(r".load"),
           add_inputs(loadGreatPromoters,uploadEnsGenes),
           r".annotated.bed")
def regulatedTables(infiles, outfile):
    '''Make an informative table about peaks and "regulated" genes'''
    
    regulated, great, ensGenes = [ P.to_table(x) for x in infiles ]

    query = '''select distinct r.contig,
                  r.start, r.end, r.peak_id, r.peak_score,
                  r.no_peaks, r.peak_width, r.peak_centre,
                  g.gene_id, e.gene_name, e.strand,
                  e.start, e.end
                  from %s as r
                  inner join %s as g
                     on g.gene_id = r.gene_id 
                  inner join %s as e
                     on g.gene_id = e.gene_id
                  ''' % (regulated, great, ensGenes)

    dbh = sqlite3.connect(db)
    cc = dbh.cursor()
    sqlresult = cc.execute(query).fetchall()

    sql_table = outfile.replace(".bed", ".txt")
    
    o = open(sql_table,"w")
    o.write("\t".join ( 
            ["chromosome","peak_start","peak_end","peak_id","peak_score",
             "no_peaks","peak_width","peak_centre",
             "dist2peak","gene_id", "TSS"]) + "\n" )

    for r in sqlresult:
        contig, pstart, pend, peak_id, peak_score, no_peaks, peak_width, peak_centre = r[0:8]
        gene_id, gene_name, gene_strand, gene_start, gene_end = r[8:14]
        
        if gene_strand == "+": gstrand = 1
        else: gstrand = 2

        tss = getTSS(gene_start,gene_end,gene_strand)

        pwidth = max(pstart,pend) - min(pstart,pend)
        ploc = (pstart + pend)/2

        if gstrand==1: tssdist = tss - ploc
        else: tssdist = ploc - tss

        columns = [ str(x) for x in
                    [  contig, pstart, pend, peak_id, peak_score, peak_width, peak_centre, tssdist, gene_id, tss] ]
        o.write("\t".join( columns  ) + "\n")
    o.close()

    # get closest genes 2 peaks, 1 gene per peak
    tmp_dir = "$SCRATCH_DIR"
    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                tail -n +2 %(sql_table)s  | sed 's/-//g' 
                | awk 'BEGIN {OFS="\\t"} {print $4,$5,$6,$7,$8,$9,$10,$1,$2,$3}' 
                | sort -k8,8 -k9,9n -k7,7n 
                | cat | uniq -f7 > $tmp 
                && awk 'BEGIN {OFS="\\t"} {print $8,$9,$10,$1,$2,$3,$4,$5,$6,$7}' $tmp 
                > %(outfile)s  && rm %(sql_table)s $tmp''' % locals()

    P.run(statement)

    
@transform(regulatedTables, suffix(".bed"), ".load")
def loadRegulatedTables(infile,outfile):
    P.load(infile,outfile,
           options='-H"contig,peak_start,peak_end,peak_id,peak_score,peak_width,peak_centre,TSSdist,gene_id,TSS" -i "peak_id" ')


@follows(loadRegulatedTables)
def great():
    pass


########################################################
####     Differential Accessibility Read Counts     ####
########################################################
@follows(great)
@transform("bowtie2.dir/*.prep.bam",
           regex(r"(.*).prep.bam"),
           r"\1.prep.bam.bai")
def indexBAM(infile, outfile):
    '''Index input BAM files'''

    statement = '''samtools index %(infile)s %(outfile)s'''

    P.run(statement)

    
def generate_scoreIntervalsBAM_jobs():
    
    # list of bed files & bam files, from which to create jobs
    intervals = glob.glob("regulated_genes.dir/*annotated.bed")
    bams = glob.glob("bowtie2.dir/*.prep.bam")

    outDir = "BAM_counts.dir/"

    for interval in intervals:
        #print interval
        ifile = [i.split("/")[-1][:-len(".annotated.bed")] for i in [interval] ]
        # iterate over intervals generating infiles & partial filenames

        for bam in bams:
            bfile = [b.split("/")[-1][:-len(".prep.bam")] for b in [bam] ]
            # for each interval, iterate over bams generating infiles & partial filenames
            bedfile = ' '.join(str(x) for x in ifile )
            bamfile = ' '.join(str(x) for x in bfile )

            output = outDir + bedfile + "." + bamfile + "_counts.txt"
            # output = outfiles. 1 for each bed/bam combination

            yield ( [ [interval, bam], output ] )

            
@follows(indexBAM)
@files(generate_scoreIntervalsBAM_jobs)
def scoreIntervalsBAM(infiles, outfile):
    '''use bedtools to count reads in bed intervals'''

    interval, bam = infiles

    tmp_dir = "$SCRATCH_DIR"
    
    if bamtools.is_paired(bam):
        statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                       cut -f1-7,9 %(interval)s > $tmp &&
                       bedtools multicov -p -q 10 -bams %(bam)s -bed $tmp > %(outfile)s &&
                       sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\tpeak_center\\tgene_id\\ttotal' %(outfile)s &&
                       rm $tmp''' % locals()
        
    else:
         statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                        cut -f1-7,9 %(interval)s > $tmp &&
                        bedtools multicov -q 10 -bams %(bam)s -bed $tmp > %(outfile)s && 
                        sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\tpeak_center\\tgene_id\\ttotal' %(outfile)s &&
                        rm $tmp''' % locals()

    P.run(statement)

    
@transform(scoreIntervalsBAM,
           regex(r"(.*).counts.txt"),
           r"\1_norm_counts.txt")
def normaliseBAMcounts(infile, outfile):
    '''normalise BAM counts for file size'''
       
    counts = pd.read_csv(infile, sep="\t", header=0)

    if "size_filt" in infile:
        name = os.path.basename(infile).replace("_counts.txt", "").replace(".", "_").lstrip("merged_peaks_GREAT_")
    else:
        name = os.path.basename(infile).rstrip(".counts.txt").split(".")[-1] + "prep"
        

    if Unpaired == False:
        query = '''select properly_paired/2 as total from flagstats where QC_status = "pass" and sample_id = "%(name)s" ''' % locals()
    else:
        query = '''select mapped as total from flagstats where QC_status = "pass" and sample_id = "%(name)s" ''' % locals()

    total_counts = fetch_DataFrame(query, db)

    norm_factor = float(total_counts["total"])/1000000 # total_counts/1x10^6

    counts["RPM"] = counts["total"].apply(lambda x: x/norm_factor)
    counts["RPM_width_norm"] = counts.apply(lambda x: x.RPM/x.peak_width if x.peak_width > 0 else x.RPM/1, axis=1)
    counts["sample_id"] = name.rstrip("_prep")
    counts["size_filt"] = counts.apply(lambda x: "<150bp" if "size_filt" in x.sample_id else "all_fragments", axis=1)
    counts["sample_id"] = counts["sample_id"].apply(lambda x: x.strip("_size_filt"))
    
    counts.to_csv(outfile, sep="\t", header=True, index=False)

                
@follows(normaliseBAMcounts)
@merge("BAM_counts.dir/*_norm_counts.txt", "all_norm_counts.txt")
def mergeNormCounts(infiles, outfile):

    infiles = [x for x in infiles if "GREAT" in x] # hack, filter preventing inclusion of counts for FRIP if running pipeline out of sequence
    head = infiles[0]
    infiles = ' '.join(infiles)

    tmp_dir = "$SCRATCH_DIR"
    statement = '''tmp=`mktemp -p %(tmp_dir)s` &&
                   head -n 1 %(head)s >  $tmp &&
                   for i in %(infiles)s; do tail -n +2 $i >> $tmp; done;
                   mv $tmp %(outfile)s'''
    
    P.run(statement)
    

@transform(mergeNormCounts, suffix(r".txt"), r".load")
def loadmergeNormCounts(infile, outfile):
    P.load(infile, outfile, options='-i "peak_id" ')

@follows(loadmergeNormCounts)
def count():
    pass


########################################################
####              Coverage tracks                   ####
########################################################
@follows(count, mkdir("deeptools.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex(r"(.*).bam"),
           r"\1.bam.bai")
def indexPrepBam(infile, outfile):
    '''samtools index bam'''

    statement = '''samtools index -b %(infile)s %(outfile)s'''

    P.run(statement)

    
@follows(indexPrepBam)
@transform("bowtie2.dir/*.prep.bam",
           regex(r"bowtie2.dir/(.*).prep.bam"),
           r"deeptools.dir/\1.coverage.bw")
def bamCoverage(infile, outfile):
    '''Make normalised bigwig tracks with deeptools'''

    if bamtools.is_paired(infile):

        # PE reads filtered on sam flag 66 -> include only first read in properly mapped pairs
        
        statement = '''bamCoverage -b %(infile)s -o %(outfile)s
                    --binSize 5
                    --smoothLength 20
                    --centerReads
                    --normalizeUsing RPKM
                    --samFlagInclude 66
                    -p "max"'''
        
    else:

        # SE reads filtered on sam flag 4 -> exclude unmapped reads
        
        statement = '''bamCoverage -b %(infile)s -o %(outfile)s
                    --binSize 5
                    --smoothLength 20
                    --centerReads
                    --normalizeUsing RPKM
                    --samFlagExclude 4
                    -p "max"'''

    # added smoothLength = 20 to try and get better looking plots...
    # --minMappingQuality 10 optional argument, but unnecessary as bams are alredy filtered
    # centerReads option and small binsize should increase resolution around enriched areas

    P.run(statement, job_memory = "2G", job_threads=20)


def generator_bamCoverage_mononuc():
    bams = glob.glob("bowtie2.dir/*prep.bam")
    if len(bams)==0:
        pass
    bams = [x for x in bams if "size_filt" not in x]

    for infile in bams:
        outfile = "deeptools.dir/" + os.path.basename(infile).replace(".prep.bam", ".nucleosome.coverage.bw")

        yield([infile, outfile])

        
@follows(indexPrepBam)
@active_if(Unpaired == False)
@files(generator_bamCoverage_mononuc)
def bamCoverage_mononuc(infile, outfile):
    '''Make normalised bigwig tracks with deeptools'''

    if bamtools.is_paired(infile):
        
        statement = '''bamCoverage -b %(infile)s -o %(outfile)s
                    --binSize 5
                    --minFragmentLength 150
                    --maxFragmentLength 300
                    --smoothLength 20
                    --centerReads
                    --normalizeUsing RPKM
                    --samFlagInclude 66
                    -p "max"'''
        
    else:
        
        statement = '''echo "Error - BAM must be PE to use --maxFragmentLength parameter" > %(outfile)'''
    
    P.run(statement, job_memory="2G", job_threads=20)

    
@follows(bamCoverage)
def coverage():
    pass


@follows(coverage)
@files(None, "regulated_genes.dir/TSS.bed")
def TSSbed(infile, outfile):
    '''Get TSSs for all genes'''
    
    query = '''select distinct contig, start, end, gene_name, strand
                  from ensemblGeneset''' 

    dbh = sqlite3.connect(db)
    cc = dbh.cursor()
    sqlresult = cc.execute(query).fetchall()

    o = open(outfile,"w")
    
    for r in sqlresult:
        contig, start, end, gene_id, gene_strand = r[0:5]
       
        if gene_strand == "+": gstrand = 1
        else: gstrand = 2

        tss = getTSS(start,end,gene_strand)

        tss_start = tss -1
        tss_end = tss +1
        
        columns = [ str(x) for x in
                    [  contig, tss_start, tss_end, gene_id] ]
        o.write("\t".join( columns  ) + "\n")
    o.close()

    
@transform(TSSbed,
           regex("regulated_genes.dir/TSS.bed"),
           add_inputs("deeptools.dir/*.coverage.bw"),
           ["deeptools.dir/TSS.all.matrix.gz", "deeptools.dir/TSS.size_filt.matrix.gz"])
def TSSmatrix(infiles, outfiles):

    bed = infiles[0]
    bws = [x for x in infiles if ".bw" in x]
    job_threads = len(bws)
    
    all_reads = ' '.join([x for x in bws if "size_filt" not in x])
    size_filt = ' '.join([x for x in bws if "size_filt" in x])

    jobs = [all_reads, size_filt]

    names = ' '.join(list(set([os.path.basename(x).split(".")[0] for x in bws ]) ) )
    
    n = 0
    for job in jobs:
        n = n + 1
        c = n -1

        outfile = outfiles[c]
        
        statement = '''computeMatrix reference-point
                         -S %(job)s
                         -R %(bed)s
                         --missingDataAsZero
                         -bs 10
                         -a 2500
                         -b 2500
                         -p "max"
                         --samplesLabel %(names)s
                         -out %(outfile)s'''

        P.run(statement)

    
@transform(TSSmatrix,
           regex("deeptools.dir/TSS.(.*).matrix.gz"),
           ["deeptools.dir/TSS.all.profile.png", "deeptools.dir/TSS.size_filt.profile.png"])
def TSSprofile(infiles, outfiles):
    '''Plot profile over TSS'''

    bws = glob.glob("deeptools.dir/*.coverage.bw")
    if len(bws) <= 6:
        opts = "--perGroup"
    else:
        opts = " "
        
    n = 0
    for infile in infiles:
        n = n + 1
        c = n - 1

        titles = ["All fragments", "Size Filter"]
        title = "TSS enrichment - " + ''.join(titles[c])
        
        outfile = outfiles[c]
        
        statement = '''plotProfile
                           -m %(infile)s
                           %(opts)s
                           --plotTitle "%(title)s"
                           --regionsLabel ""
                           --yAxisLabel "ATAC signal (RPKM)"
                           -out %(outfile)s'''

        P.run(statement)

        
@transform(TSSmatrix,
           regex("deeptools.dir/TSS.(.*).matrix.gz"),
           ["deeptools.dir/TSS.all.heatmap.png", "deeptools.dir/TSS.size_filt.heatmap.png"])
def TSSheatmap(infiles, outfiles):
    '''Plot profile over TSS'''

    bws = glob.glob("deeptools.dir/*.coverage.bw")

    n = 0
    for infile in infiles:
        n = n + 1
        c = n - 1

        titles = ["All fragments", "Size Filter"]
        title = "TSS enrichment - " + ''.join(titles[c])
        
        outfile = outfiles[c]
        
        statement = '''plotHeatmap
                           -m %(infile)s
                           --plotTitle "%(title)s"
                           --regionsLabel ""
                           --xAxisLabel ""
                           --yAxisLabel "ATAC signal (RPKM)"
                           --heatmapHeight 10
                           -out %(outfile)s'''

        P.run(statement)

        
@follows(TSSprofile, TSSheatmap)
def TSSplot():
    pass        


@follows(TSSplot)
@files(None, "*.nbconvert.html")
def report(infile, outfile):
    '''Generate html report on pipeline results from ipynb template(s)'''

    templates = PARAMS["report_path"]
    templates = templates.split(",")

    if len(templates)==0:
        print("Specify Jupyter ipynb template path in pipeline.ini for html report generation")
        pass

    for template in templates:
        infile = os.path.basename(template)
        outfile = infile.replace(".ipynb", ".nbconvert.html")
        nbconvert = infile.replace(".ipynb", ".nbconvert.ipynb")
        tmp = os.path.basename(template)
    
        statement = '''cp %(template)s .  &&
                   jupyter nbconvert 
                     --to notebook 
                     --allow-errors 
                     --ExecutePreprocessor.timeout=360
                     --execute %(infile)s && 
                   jupyter nbconvert 
                     --to html 
                     --ExecutePreprocessor.timeout=360
                     --execute %(nbconvert)s &&
                   rm %(tmp)s'''

        P. run()
    

# ---------------------------------------------------
# Generic pipeline tasks
@follows(mapping, peakcalling, coverage, frip, count, TSSplot)
def full():
    pass

def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)
                        
if __name__ == "__main__":
    sys.exit(P.main(sys.argv))

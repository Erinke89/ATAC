'''###################################################
#                                                    #
#                   Pipeline ATAC                    #
#                                                    #
######################################################

Pipeline for analysis of ATAC-seq data

Tasks:
1) mapping
   - Bowtie2
   - Duplicate removal
   - Insert size filtering
   - Collect QC metrics
2) peakcalling
   - Macs2 callpeak
   - Subtract blacklists
   - Merge replicate peaks
   - Annotate peaks to genes
   - QC
3) counting
   - Make consensus peakset of all detected peaks
   - Count reads over consensus peakset
   - Normalise counts
4) bigwigs
   - Prepare bigWigs for visualisation
   - Plot coverage at TSS's
5) report
   - run jupyter notebook reports


Inputs:
   - Fastq files (paired or single end)
   - fastq files should be named as in the following format:
        * cell/tissue_condition_treatment_replicate.fastq.[1-2].gz (PE)
        * cell/tissue_condition_treatment_replicate.fastq.[1-2].gz (SE)

        * if fewer categories are needed to describe samples they may be named
           ~ condition_treatment_replicate.fastq*.gz
           ~ condition_replicate.fastq*gz

        *** it is important that the naming convention is followed otherwise downstream tasks will fail ***

   - and placed in data.dir

Configuration
   - Pipeline configuration should be specified in the pipeline.yml

Outputs
   - mapped reads, filtered by insert size
   - called peaks, merged peaks (by replicates)
   - read counts, for differential accessibility testing
   - coverage tracks, for visualisation
   - QC, mapping, peakcalling, signal:background
   - reports, data exploration and differential accessibility


######################################################
'''

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

import PipelineAtac as A

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

# ---------------------------------------------------
# Configure pipeline global variables
Unpaired = A.isPaired(glob.glob("data.dir/*fastq*gz"))

#####################################################
####            Sample Info Table                ####
#####################################################
@follows(connect)
@files(None, "sample_info.txt")
def makeSampleInfoTable(infile, outfile):
    '''Parse sample names and construct sample info table,
       with "category" column for DESeq2 design'''
    
    make_sample_table = True
    info = {}

    files = glob.glob("data.dir/*fastq*gz")
    
    if len(files)==0:
        pass
    
    for f in files:
        sample_id = os.path.basename(f).split(".")[0]
        attr =  os.path.basename(f).split(".")[0].split("_")

        if len(attr) == 2:
            cols = ["sample_id", "condition", "replicate"]

        elif len(attr) == 3:
            cols = ["sample_id", "condition", "treatment", "replicate"]

        elif len(attr) == 4:
            cols = ["sample_id", "group", "condition", "treatment", "replicate"]

        else:
            make_sample_table = False
            print("Please reformat sample names according to pipeline documentation")

        if sample_id not in info:
            info[sample_id] = [sample_id] + attr

    if make_sample_table:
        sample_info = pd.DataFrame.from_dict(info, orient="index")
        sample_info.columns = cols
        sub = [x for x in list(sample_info) if x not in ["sample_id", "index", "replicate"]]
        sample_info["category"] = sample_info[sub].apply(lambda x: '_'.join(str(y) for y in x), axis=1)
        sample_info.reset_index(inplace=True, drop=True)
        
        con = sqlite3.connect(db)
        sample_info.to_sql("sample_info", con, if_exists="replace")

        sample_info.to_csv(outfile, sep="\t", header=True, index=False)


#####################################################
####                Mapping                      ####
#####################################################
@follows(makeSampleInfoTable, mkdir("bowtie2.dir"))
@transform("data.dir/*.fastq.1.gz",
           regex(r"data.dir/(.*).fastq.1.gz"),
           r"bowtie2.dir/\1.genome.bam")
def mapBowtie2_PE(infile, outfile):
    '''Map reads with Bowtie2'''
    
    if len(infile) == 0:
        pass

    read1 = infile
    read2 = infile.replace(".1.gz", ".2.gz")

    log = outfile + "_bowtie2.log"
    tmp_dir = "$SCRATCH_DIR"

    options = PARAMS["bowtie2_options"]
    genome = os.path.join(PARAMS["bowtie2_genomedir"], PARAMS["bowtie2_genome"])

    statement = f'''tmp=`mktemp -p {tmp_dir}` && 
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x {genome}
                     -1 {read1} 
                     -2 {read2}
                     {options}
                     1> $tmp 
                     2> {log} && 
                   samtools sort -O BAM -o {outfile} $tmp && 
                   samtools index {outfile} && 
                   rm $tmp'''

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

    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x {genome}
                     -U {infile} 
                     {options}
                     1> $tmp 
                     2> {log} &&
                   samtools sort -O BAM -o {outfile} $tmp &&
                   samtools index {outfile} &&
                   rm $tmp'''

    P.run(statement,job_memory="2G",job_threads=12)

    
@follows(mapBowtie2_PE, mapBowtie2_SE)
@transform("bowtie2.dir/*.genome.bam", suffix(r".genome.bam"), r".filt.bam")
def filterBam(infile, outfile):
    '''filter bams on MAPQ >10, & remove reads mapping to chrM before peakcalling'''

    local_tmpdir = "/gfs/scratch/"
        
    statement = f'''tmp=`mktemp -p {local_tmpdir}` && 
                   head=`mktemp -p {local_tmpdir}` &&
                   samtools view -h {infile} | grep "^@" - > $head  && 
                   samtools view -q10 {infile} | 
                     grep -v "chrM" - | 
                     cat $head - |
                     samtools view -h -o $tmp -  && 
                   samtools sort -O BAM -o {outfile} $tmp  &&
                   samtools index {outfile} &&
                   rm $tmp $head'''  

    P.run(statement, job_memory="10G", job_threads=2)
    

@transform(filterBam,
           regex(r"(.*).filt.bam"),
           r"\1.all.prep.bam")
def removeDuplicates(infile, outfile):
    '''PicardTools remove duplicates'''

    metrics_file = outfile + ".picardmetrics"
    log = outfile + ".picardlog"
    tmp_dir = "$SCRATCH_DIR"

    statement = f'''tmp=`mktemp -p {tmp_dir}` && 
                   MarkDuplicates 
                     INPUT={infile} 
                     ASSUME_SORTED=true 
                     REMOVE_DUPLICATES=true 
                     QUIET=true 
                     OUTPUT=$tmp 
                     METRICS_FILE={metrics_file} 
                     VALIDATION_STRINGENCY=SILENT
                     TMP_DIR=/gfs/scratch/
                     2> {log}  && 
                   mv $tmp {outfile} && 
                   samtools index {outfile}'''

    P.run(statement, job_memory="12G", job_threads=2)

    
@active_if(Unpaired == False)
@transform(removeDuplicates,
           suffix(r".all.prep.bam"),
           r".size_filt.prep.bam")
def size_filterBam(infile, outfile):
    '''filter bams on insert size (max size specified in ini)'''

    local_tmpdir = "$SCRATCH_DIR"

    insert_size_filter_F = PARAMS["bowtie2_insert_size"]
    insert_size_filter_R = "-" + str(insert_size_filter_F) # reverse reads have "-" prefix for TLEN

    statement = f'''tmp=`mktemp -p {local_tmpdir}` && 
                   head=`mktemp -p {local_tmpdir}` && 
                   samtools view -h {infile} | grep "^@" - > $head  && 
                   samtools view {infile} | 
                     awk 'BEGIN {{OFS="\\t"}} {{if ($9 ~ /^-/ && $9 > {insert_size_filter_R}) print $0 ;
                       else if ($9 ~ /^[0-9]/ && $9 < {insert_size_filter_F}) print $0}}' - |     
                     cat $head - |
                     samtools view -h -o $tmp -  && 
                   samtools sort -O BAM -o {outfile} $tmp  && 
                   samtools index {outfile} &&
                   rm $tmp $head'''  

    P.run(statement, job_memory="10G", job_threads=2)

    
@follows(size_filterBam)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".bam.bai")
def indexBam(infile, outfile):
    '''index bams, if index failed to be generated'''

    statement = f'''samtools index -b {infile} > {outfile}'''

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
    
    statement =  f'''tmp=`mktemp -p {tmp_dir}` && 
                    samtools idxstats {infile} > $tmp &&
                    awk 'BEGIN {{OFS="\\t"}} {{print $0,"{name}"}}' $tmp > {outfile} &&
                    rm $tmp'''

    P.run(statement)


@follows(contigReadCounts)
@merge("bowtie2.dir/*.contigs.counts", "allContig.counts")
def mergeContigCounts(infiles, outfile):

    infiles = ' '.join(infiles)
    
    statement = f'''cat {infiles} > {outfile}'''

    P.run(statement)

    
@transform(mergeContigCounts, suffix(r".counts"), r".load")
def loadmergeContigCounts(infile, outfile):

    P.load(infile, outfile, options='-H "contig,length,mapped_reads,unmapped_reads,sample_id" ')

    
@follows(loadmergeContigCounts)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".flagstats.txt")
def flagstatBam(infile, outfile):
    '''get samtools flagstats for bams'''

    statement = f'''samtools flagstat {infile} > {outfile}'''
    
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
           regex(r"(.*).bam"),
           r"\1.picardAlignmentStats.txt")
def picardAlignmentSummary(infile, outfile):
    '''get aligntment summary stats with picard'''

    tmp_dir = "$SCRATCH_DIR"
    refSeq = os.path.join(PARAMS["genome_dir"], PARAMS["genome"] + ".fa")
    
    statement = f'''tmp=`mktemp -p {tmp_dir}` && 
                   java -Xms12G -Xmx14G -jar /gfs/apps/bio/picard-tools-2.15.0/picard.jar CollectAlignmentSummaryMetrics
                     R={refSeq}
                     I={infile}
                     O=$tmp && 
                   cat $tmp | grep -v "#" > {outfile}'''

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
           regex(r"(.*).bam"),
           r"\1.picardInsertSizeMetrics.txt")
def picardInsertSizes(infile, outfile):
    '''get aligntment summary stats with picard'''

    tmp_dir = "$SCRATCH_DIR"

    pdf = outfile.replace("Metrics.txt", "Histogram.pdf")
    histogram = outfile.replace("Metrics.txt", "Histogram.txt")
    
    statement = f'''tmp=`mktemp -p {tmp_dir}` && 
                   java -Xms12G -Xmx45G -jar /gfs/apps/bio/picard-tools-2.15.0/picard.jar CollectInsertSizeMetrics
                     TMP_DIR=/gfs/scratch/
                     I={infile} 
                     O=$tmp
                     H={pdf}
                     M=0.5 && 
                   cat $tmp | 
                     grep -A`wc -l $tmp | 
                     tr "[[:blank:]]" "\\n" | 
                     head -n 1` "## HISTOGRAM" $tmp | 
                     grep -v "#" > {histogram} &&
                   cat $tmp | 
                     grep -A 2 "## METRICS CLASS" $tmp | 
                     grep -v "#" > {outfile} &&
                   rm $tmp'''

    
    P.run(statement, job_memory="10G", job_threads=5)

    
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

# MACS2 has now been updated for python3
# TODO:
#    - test pipeline with new version of MACS2 (2.2.6)
#    - all parameters appear the same so should be no problems
#    - if working with new version of MACS2 modify statements to run
#      in normal environment (no longer need python2 conda env for MACS2)

@active_if(Unpaired)
@follows(mapping, mkdir("macs2.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex("bowtie2.dir/(.*).prep.bam"),
           r"macs2.dir/\1.macs2.fragment_size.tsv")
def macs2Predictd(infile, outfile):
    '''predict fragment sizes for SE ChIP'''

    options = PARAMS["macs2_se_options"]
    outdir = os.path.dirname(outfile)
    
    statement = f'''macs2 predictd 
                     --format BAM 
                     --ifile {infile} 
                     --outdir {outdir} 
                     --verbose 2 {options} 
                     2> {outfile}'''

    P.run(statement, job_threads=4)

    
@active_if(Unpaired)
@transform(macs2Predictd, suffix(r".fragment_size.tsv"), r".fragment_size.txt")
def getFragmentSize(infile, outfile):
    '''Get fragment sizes from macs2 predictd'''

    sample = os.path.basename(infile).rstrip(".fragment_size.tsv")
    tmp_dir = "$SCRATCH_DIR"
    
    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                   echo {sample} `cat {infile} | 
                     grep "# tag size =" | 
                     tr -s " " "\\t" | 
                     awk 'BEGIN {{OFS="\\t "}} {{print $12}}'` > $tmp && 
                   cat $tmp | 
                     tr -s " " "\\t" > {outfile} &&
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
        
        statement = f'''macs2 callpeak 
                          --outdir macs2.dir                  
                          --bdg
                          --SPMR
                          {options} 
                          --treatment {infile} 
                          --name {name} 
                          >& {outfile}'''  

    else:
        # get macs2 predictd fragment lengths from csvdb
        table = os.path.basename(infile).replace(".fastq.1.gz", ".macs2.fragment_size").replace("-", "_").replace(".", "_")

        query = f'''select tag_size from {table} '''  

        dbh = sqlite3.connect(db)
        cc = dbh.cursor()
        sqlresult = cc.execute(query).fetchall()

        fragment_length = sqlresult[0]
        fragment_length = fragment_length[0]

        # run macs2 callpeak
        options = PARAMS["macs2_se_options"]
        name = os.path.basename(outfile).split(".")[0]
        tmp_dir = "$SCRATCH_DIR"

        statement = f'''macs2 callpeak 
                          --outdir macs2.dir 
                          --bdg
                          --SPMR
                          {options} 
                          --treatment {infile} 
                          --name {name} 
                          >& {outfile}'''  
    
    P.run(statement, job_threads=5)

    
@follows(macs2callpeaks)
@files(None, "blacklist_chip.mm10.bed.gz")
def getChIPblacklist(infile, outfile):
    '''Get Ensembl ChIP blacklisted regions'''

    chip_blacklist = PARAMS["peak_filter_chip_blacklist"]
    statement = f'''wget -O {outfile} {chip_blacklist}'''

    P.run(statement)

    
@follows(getChIPblacklist)
@files(None, "blacklist_atac.mm10.bed.gz")
def getATACblacklist(infile, outfile):
    '''Get ATAC blacklist regions'''

    atac_blacklist = PARAMS["peak_filter_atac_blacklist"]
    
    statement = f'''wget -q {atac_blacklist} | 
                      gzip - > {outfile}'''

    P.run(statement)

    
@follows(getATACblacklist)
@transform("macs2.dir/*_peaks.narrowPeak",
           regex("(.*)_peaks.narrowPeak"),
           add_inputs([getChIPblacklist, getATACblacklist]),
           r"\1.peaks.bed")
def filterPeaks(infiles, outfile):
    '''subtract blacklist regions from peakset'''

    peak, blacklists = infiles

    blacklist = ' '.join(blacklists)
    
    statement = f'''intersectBed 
                      -wa 
                      -v 
                      -a {peak} 
                      -b <(zcat {blacklist} ) 
                      > {outfile}'''
    
    P.run(statement)


def mergeReplicatePeaksGenerator():
    '''Get replicate info from pipeline.ini & create jobs'''

    if PARAMS["replicates_auto_merge"]:
        peaksets = [".all", ".size_filt"]

        for peaks in peaksets:

            pfiles = glob.glob("macs2.dir/*.peaks.bed")
            
            if len(pfiles)==0:
                pass

            if peaks ==".all":
                pfiles = [x for x in pfiles if "size_filt" not in x]
            else:
                pfiles = [x for x in pfiles if "size_filt" in x]

            reps = {}
            for p in pfiles:
                m = re.match(r"macs2.dir/(.*)_r*([1-9])(.*).peaks.bed", p)
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

            for reps in replicates:

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

    rep_overlaps = PARAMS["replicates_overlap"]
    
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

                # get unique elements from list of replicates by converting to set
                if len(set(peak_id.split(","))) >= rep_overlaps:
                    # outfile
                    bed = [contig, start, end, peak_id, peak_score, strand, FC, pval, qval, summit]
                    bed = '\t'.join(bed) + '\n'

                    output.write(bed)

    statement = f'''rm {tmp}'''

    P.run(statement)


@follows(mergeReplicatePeaks)
@files(None, "macs2.dir/no_peaks.txt")
def countPeaks(infiles, outfile):

    beds = glob.glob("./macs2.dir/*.peaks.bed")
    merge_beds = glob.glob("./macs2.dir/*.merged.bed")

    peaksets = [beds, merge_beds]

    if len(peaksets)==0:
        pass
    
    no_peaks = {}

    for peaks in peaksets:
        for bed in peaks:
            if "merged" in bed:
                name = '.'.join(os.path.basename(bed).split(".")[0:3]).replace(".all", "")
            else:
                name = os.path.basename(bed).replace(".peaks.bed", "")

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
    
    statement = f'''blacklist=`mktemp -p $SCRATCH_DIR` &&
                   zcat {blacklists} > $blacklist &&
                   java -Xms20G -Xmx40G -jar {exe} 
                     -b {infile}
                     -i {index}
                     -g {contigs}
                     -o {name}
                     {options}
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
    all_intervals = glob.glob("macs2.dir/*.peaks.bed")
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
        intervals = [x for x in group if ".bed" in x]
        bams = [x for x in group if "prep.bam" in x]

        for interval in intervals:
            match = ''.join(['.'.join(os.path.basename(i).split(".")[0:2]) for i in [interval] ])
            for bam in bams:
                bam_sample = bam.split("/")[-1].replace(".prep.bam", "")
                
                if match in bam and match in interval:
                    bfile = ''.join([''.join(os.path.basename(b).split(".")[0:2]) for b in bam ])
                    output = outDir + match + ".fripcounts" + ".txt"
                    
                    yield [ [interval, bam], output ] 

                        
@follows(mkdir("FRIP.dir"), peakcalling)
@files(generate_FRIPcountBAM_jobs)
def FRIPcountBAM(infiles, outfile):
    '''use bedtools to count reads in bed intervals'''

    interval, bam = infiles

    if bamtools.is_paired(bam):
         # -p flag specifes only to count paired reads

        statement = f'''bedtools multicov -p -q 10 -bams {bam} 
                    -bed <( awk 'BEGIN {{OFS="\\t"}} {{print $1,$2,$3,$4,$5,$3-$2}}' {interval} ) 
                    > {outfile} 
                    && sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\ttotal' 
                    {outfile}'''  

    else:

         statement = f'''bedtools multicov -q 10 -bams {bam} 
                     -bed <( awk 'BEGIN {{OFS="\\t"}} {{print $1,$2,$3,$4,$5,$3-$2}}' {interval} ) 
                     > {outfile} 
                     && sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\ttotal' 
                     {outfile} '''  

    print(statement)
         
    P.run(statement)

    
@transform(FRIPcountBAM,
           regex(r"(.*).fripcounts.txt"),
           r"\1.frip.txt")
def FRIP(infile, outfile):
        '''Calculate fraction of read in peaks'''

        insert_size = PARAMS["bowtie2_insert_size"]
        
        if "size_filt" in infile:
            size_filt = '''"<''' + str(insert_size) + '''bp"'''
        else:
            size_filt = "all_fragments"

        sample_name = os.path.basename(infile).replace(".fripcounts.txt", "")
        sample_label = sample_name.split(".")[0]
        bam = "bowtie2.dir/" + sample_name + ".prep.bam"
        

        statement = f'''total_reads=`samtools view {bam} | 
                         wc -l` &&
                       peak_reads=`awk 'BEGIN {{OFS="\\t"}}
                         {{sum += $7;}} END 
                         {{print sum;}}' {infile}` &&
                       awk -v sample={sample_label} 
                         -v isize={size_filt}
                         -v t_reads=$total_reads
                         -v p_reads=$peak_reads
                         'BEGIN {{print p_reads / t_reads, sample, isize}}' | 
                         tr -s "[[:blank:]]" "\\t" 
                         > {outfile}'''

        print(statement)

        P.run(statement)

@merge(FRIP, "FRIP.dir/frip_table.txt")
def FRIP_table(infiles, outfile):
    '''merge all files to load to csvdb'''    

    infiles = ' '.join(infiles)

    tmp_dir = "$SCRATCH_DIR"
    
    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                   awk 'BEGIN {{printf "FRIP\\tsample_id\\tsize_filt\\n"}}' > $tmp &&
                   for i in {infiles}; 
                     do cat $i >> $tmp; done;
                   mv $tmp {outfile}'''

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
@merge("macs2.dir/*.peaks.bed", "BAM_counts.dir/merged_peaks.bed")
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

    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                   cat {infiles} | grep -v ^chrUn* - > $tmp &&
                   awk 'BEGIN {{OFS="\\t"}} {{center = $2 + $10 ; start = center - {offset} ; end = center + {offset} ;
                     print $1,start,end,$4,$5}}' $tmp | 
                   awk 'BEGIN {{OFS="\\t"}} {{if (($2 < $3) && ($2 > 0)) print $0}}' - |
                     sort -k1,1 -k2,2n |
                     mergeBed -c 4,5 -o count,mean -i - |
                     awk 'BEGIN {{OFS="\\t"}} {{print $1,$2,$3,"merged_peaks_"NR,$5,$4,$3-$2,sprintf("%%i", ($2+$3)/2)}}' - > {outfile} &&
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

    statement = '''select a.gene_id, a.gene_name, b.contig, min(b.start) as start, max(b.end) as end, b.strand
                     from gene_info a 
                     inner join geneset_all_gtf b 
                     on a.gene_id = b.gene_id 
                     where b.gene_biotype = "protein_coding" and b.strand = "+" 
                     group by b.gene_id 
                   union 
                   select a.gene_id, a.gene_name, b.contig, max(b.start) as start, min(b.end) as end, b.strand
                     from gene_info a 
                     inner join geneset_all_gtf b on a.gene_id = b.gene_id 
                     where b.gene_biotype = "protein_coding" and b.strand = "-" group by b.gene_id'''

    anndb = os.path.join(PARAMS["annotations_dir"], "csvdb")
    
    df = A.fetch_DataFrame(statement, anndb)
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
    
    A.writeGreat(locations,basalup,basaldown,maxext,outfile,half)


@transform(greatPromoters,
           regex(r"(.*)_prom.bed"),
           r"\1.bed")
def filterEnsPromoters(infile,outfile):
    '''Remove unwanted chromosomes & correct contig column, "chrchr" -> "chr"'''

    tmp_dir = "$SCRATCH_DIR"
    statement = f'''tmp=`mktemp -p {tmp_dir}` && 
                    sed 's/chrchr/chr/' {infile} > $tmp &&
                    grep -v ^chrM $tmp > {outfile} && 
                    rm $tmp'''

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
    statement = f'''intersectBed 
                      -wa 
                      -wb 
                      -a <(cut -f1-8 {infile}) 
                      -b {greatPromoters} | 
                      cut -f1-8,12 > {outfile}'''  

    # Filter on nearest peak 2 gene later

    P.run(statement)

    
@transform(regulatedGenes, suffix(r".txt"), r".load")
def loadRegulatedGenes(infile, outfile):
    P.load(infile, outfile, 
           options='-H "contig,start,end,peak_id,peak_score,no_peaks,peak_width,peak_centre,gene_id" -i "peak_id"')

    
@transform(loadRegulatedGenes,
           suffix(r".load"),
           add_inputs(loadGreatPromoters,uploadEnsGenes),
           r".closestGene.bed")
def regulatedTables(infiles, outfile):
    '''Make an informative table about peaks and "regulated" genes'''
    
    regulated, great, ensGenes = [ P.to_table(x) for x in infiles ]

    query = f'''select distinct r.contig,
                  r.start, r.end, r.peak_id, r.peak_score,
                  r.no_peaks, r.peak_width, r.peak_centre,
                  g.gene_id, e.gene_name, e.strand,
                  e.start, e.end
                  from {regulated} as r
                  inner join {great} as g
                     on g.gene_id = r.gene_id 
                  inner join {ensGenes} as e
                     on g.gene_id = e.gene_id'''

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

        tss = A.getTSS(gene_start,gene_end,gene_strand)

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
    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                    tail -n +2 {sql_table}  | sed 's/-//g' | 
                      awk 'BEGIN {{OFS="\\t"}} {{print $4,$5,$6,$7,$8,$9,$10,$1,$2,$3}}' |
                      sort -k8,8 -k9,9n -k7,7n |
                      cat | 
                      uniq -f7 > $tmp && 
                    awk 'BEGIN {{OFS="\\t"}} {{print $8,$9,$10,$1,$2,$3,$4,$5,$6,$7}}' $tmp > {outfile}  && 
                    rm {sql_table} $tmp'''

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

    statement = f'''samtools index {infile} {outfile}'''

    P.run(statement)

    
def generate_scoreIntervalsBAM_jobs():
    
    # list of bed files & bam files, from which to create jobs
    intervals = glob.glob("regulated_genes.dir/*closestGene.bed")
    bams = glob.glob("bowtie2.dir/*.prep.bam")

    outDir = "BAM_counts.dir/"

    for interval in intervals:
        #print interval
        ifile = [i.split("/")[-1][:-len(".closestGene.bed")] for i in [interval] ]
        # iterate over intervals generating infiles & partial filenames

        for bam in bams:
            bfile = [b.split("/")[-1][:-len(".prep.bam")] for b in [bam] ]
            # for each interval, iterate over bams generating infiles & partial filenames
            bedfile = ' '.join(str(x) for x in ifile )
            bamfile = ' '.join(str(x) for x in bfile )

            output = outDir + bedfile + "." + bamfile + ".counts.txt"
            # output = outfiles. 1 for each bed/bam combination

            yield [ [interval, bam], output ] 

            
@follows(indexBAM)
@files(generate_scoreIntervalsBAM_jobs)
def scoreIntervalsBAM(infiles, outfile):
    '''use bedtools to count reads in bed intervals'''

    interval, bam = infiles

    tmp_dir = "$SCRATCH_DIR"
    
    if bamtools.is_paired(bam):
        statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                        cut -f1-7,9 {interval} > $tmp &&
                        bedtools multicov 
                          -p 
                          -q 10 
                          -bams {bam} 
                          -bed $tmp > {outfile} &&
                        sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\tpeak_center\\tgene_id\\ttotal' {outfile} &&
                        rm $tmp'''  
        
    else:
         statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                         cut -f1-7,9 {interval} > $tmp &&
                         bedtools multicov 
                          -q 10 
                          -bams {bam} 
                          -bed $tmp > {outfile} && 
                         sed -i '1i \chromosome\\tstart\\tend\\tpeak_id\\tpeak_score\\tpeak_width\\tpeak_center\\tgene_id\\ttotal' {outfile} &&
                         rm $tmp'''  

    P.run(statement)

    
@transform(scoreIntervalsBAM,
           regex(r"(.*).counts.txt"),
           r"\1.norm_counts.txt")
def normaliseBAMcounts(infile, outfile):
    '''normalise BAM counts for file size'''

    # write this as a script and submit job to cluster
    
    counts = pd.read_csv(infile, sep="\t", header=0)

    name = os.path.basename(infile).replace(".counts.txt", "").replace("merged_peaks.GREAT.", "").replace(".", "_")

    if Unpaired == False:
        query = f'''select (properly_paired/2)/1E06 as total from flagstats where QC_status = "pass" and sample_id = "{name}" '''  
    else:
        query = f'''select mapped/1E06 as total from flagstats where QC_status = "pass" and sample_id = "{name}" '''  

    total_counts = A.fetch_DataFrame(query, db)
    norm_factor = total_counts["total"]
    
    counts["RPM"] = counts["total"].apply(lambda x: x/norm_factor)
    counts["RPM_width_norm"] = counts.apply(lambda x: x.RPM/x.peak_width if x.peak_width > 0 else x.RPM/1, axis=1)
    counts["sample_id"] = name.rstrip("_prep")
    counts["size_filt"] = counts.apply(lambda x: "<150bp" if "size_filt" in x.sample_id else "all_fragments", axis=1)
    counts["sample_id"] = counts["sample_id"].apply(lambda x: x.strip("_size_filt"))
    
    counts.to_csv(outfile, sep="\t", header=True, index=False)

                
@follows(normaliseBAMcounts)
@merge("BAM_counts.dir/*.norm_counts.txt", "all_norm_counts.txt")
def mergeNormCounts(infiles, outfile):

    head = infiles[0]
    infiles = ' '.join(infiles)

    tmp_dir = "$SCRATCH_DIR"

    statement = f'''tmp=`mktemp -p {tmp_dir}` &&
                    head -n 1 {head} >  $tmp &&
                    for i in {infiles}; 
                      do tail -n +2 $i >> $tmp; 
                      done;
                    mv $tmp {outfile}'''
    
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

    statement = f'''samtools index -b {infile} {outfile}'''

    P.run(statement)

    
@follows(indexPrepBam)
@transform("bowtie2.dir/*.prep.bam",
           regex(r"bowtie2.dir/(.*).prep.bam"),
           r"deeptools.dir/\1.coverage.bw")
def bamCoverage(infile, outfile):
    '''Make normalised bigwig tracks with deeptools'''

    if bamtools.is_paired(infile):

        # PE reads filtered on sam flag 66 -> include only first read in properly mapped pairs
        
        statement = f'''bamCoverage 
                          -b {infile} 
                          -o {outfile}
                          --binSize 5
                          --smoothLength 20
                          --centerReads
                          --normalizeUsing RPKM
                          --samFlagInclude 66
                          -p "max"'''
        
    else:

        # SE reads filtered on sam flag 4 -> exclude unmapped reads
        
        statement = f'''bamCoverage 
                          -b {infile} 
                          -o {outfile}
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

        yield [infile, outfile]

        
@follows(indexPrepBam)
@active_if(Unpaired == False)
@files(generator_bamCoverage_mononuc)
def bamCoverage_mononuc(infile, outfile):
    '''Make normalised bigwig tracks with deeptools'''

    if bamtools.is_paired(infile):
        
        statement = f'''bamCoverage 
                          -b {infile} 
                          -o {outfile}
                          --binSize 5
                          --minFragmentLength 150
                          --maxFragmentLength 300
                          --smoothLength 20
                          --centerReads
                          --normalizeUsing RPKM
                          --samFlagInclude 66
                          -p "max"'''
        
    else:
        
        statement = f'''echo "Error - BAM must be PE to use --maxFragmentLength parameter" > {outfile}'''
    
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

        tss = A.getTSS(start,end,gene_strand)

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
        
        statement = f'''computeMatrix reference-point
                          -S {job}
                          -R {bed}
                          --missingDataAsZero
                          -bs 10
                          -a 2500
                          -b 2500
                          -p "max"
                          --samplesLabel {names}
                          -out {outfile}'''

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
        
        statement = f'''plotProfile
                           -m {infile}
                           {opts}
                           --plotTitle "{title}"
                           --regionsLabel ""
                           --yAxisLabel "ATAC signal (RPKM)"
                           -out {outfile}'''

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
        
        statement = f'''plotHeatmap
                           -m {infile}
                           --plotTitle "{title}"
                           --regionsLabel ""
                           --xAxisLabel ""
                           --yAxisLabel "ATAC signal (RPKM)"
                           --heatmapHeight 10
                           -out {outfile}'''

        P.run(statement)

        
@follows(TSSprofile, TSSheatmap)
def TSSplot():
    pass        


@follows(TSSplot)
@files(None, "*.nbconvert.html")
def report(infile, outfile):
    '''Generate html report on pipeline results from ipynb template(s)'''

    templates = PARAMS["report_path"]

    if len(templates)==0:
        print("Specify Jupyter ipynb template path in pipeline.ini for html report generation")
        pass
    
    for template in templates:
        infile = os.path.basename(template)
        outfile = infile.replace(".ipynb", ".nbconvert.html")
        nbconvert = infile.replace(".ipynb", ".nbconvert.ipynb")
        tmp = os.path.basename(template)
        
        statement = f'''cp {template} .  &&
                        jupyter nbconvert 
                          --to notebook 
                          --allow-errors 
                          --ExecutePreprocessor.timeout=-1
                          --execute {infile} && 
                        jupyter nbconvert 
                          --to html 
                          --ExecutePreprocessor.timeout=-1
                          --execute {nbconvert} &&
                        rm {tmp}'''

        P.run(statement)
    

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

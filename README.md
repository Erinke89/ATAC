# ATAC
Pipelines for ATAC-seq analysis

## pipeline_atac
Pipeline for analysis of ATAC-seq data

This is for generic ATAC-seq analysis, to be run after cgatflow readqc and adaptor removal of fastq files.

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
    - ATAC_Pipeline_Report - QC and experiment overview
    - ATAC_Pipeline_DESeq2 - differential accessibility testing
   
Inputs:
    - fastq.gz formatted files. Can be paired or single end.
    - should be named sample_r1.fastq.[1-2].gz (PE) or sample_r1.fastq.gz (SE)
    - naming convention: sample names should be informative e.g. "group_condition_treatment_replicate.fastq.1.gz" 
    as they're used to generate a sample information table to annotate plots and create comparisons for DESeq2
    
Outputs:
    - bowtie2.dir: mapped and filtered BAMs
    - macs2.dir: Macs2 output, plus filtered peaks ("*.peaks.bed"), and merged peaks ("*.merged.bed")
    - DESeq2.dir: BED files contataining differentially accessible peaks
    - deeptools.dir: bigWig coverage tracks
    - csvdb: sqlite3 db containing all QC metics, read counts, peak locations, and gene annotations
    - Jupyter notebooks with standard analysis

<br>
***

## pipeline_memechip
Runs MEME-ChIP and HOMER for *de novo* motif discovery. Also, optionally runs MEME suite tools AME and MAST to search for instances of known motifs or motifs diszcovered by MEME-ChIP.

Tasks:
1) runMemeAnalysis
    - offsets peaks to peak centre +/- n b.p. 
    - gets peak flanking regions (of equal width to peaks) for local background
    - gets peak and background sequences and creates background model
    - runs MEME-ChIP
2) runHomerAnalysis
    - runs Homer findMotifs.pl with local and default (genomic) background
    - annotates peaks with motifs using Homer annotatePeaks.pl
    - plots enrichment of Homer motifs relative to peaks
3) runMotifAnalysis
    - target to run MEME and Homer tasks
4) runAme
    - run AME for enrichment of known motifs
5) runMastAnalysis
    - run MAST on MEME-ChIP results (specified in pipeline.yml)
    
Inputs:
    - BED formatted peak files e.g. output from pipeline_atac.py or Macs2. Specify the format of peak files in pipeline.yml

Outputs:
    - meme.chip.dir: MEME-ChIP results
    - homer.chip.dir: Homer results (with local background)
    - homer.genome.dir: Homer results (with genomic background)
    - meme.ame.dir: AME results
    - query_motifs.dir: MAST results for motifs of interest
    - motifsCoverage.dir: Homer motif matrices and enrichment plots


## pipeline_motifenrichment
Fimo scanning for known motifs. Creates per base pair matrix of motif occurences and plots motif frequencies over input intervals. 

## pipeline_superenhancer
Runs ROSE-style super enhancer analysis using ATAC-seq peaks as input

## pipeline_footprint
Requires high sequencing depth. Calculates cut site frequency over intervals (e.g. motif sites)

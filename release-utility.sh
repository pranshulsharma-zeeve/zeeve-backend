#! /bin/bash  
increment (){
string=$VERSION
name=""
IFS='.'
read -ra arr <<< "$string"
for val in "${arr[@]}";
do
        name=$val
done
x=1
inc=$(($name+$x))
arr[2]=$inc
delim=""
joined=""
for item in "${arr[@]}"; do
  joined="$joined$delim$item"
  delim="."
done
echo "$joined"
}

variables(){
BUILD_ENV=${deploy_env}_BUILD_ENV
echo export BUILD_ENV=${!BUILD_ENV}
ACCESS_KEY_ID="${deploy_env}_AWS_ACCESS_KEY_ID"
echo export AWS_ACCESS_KEY_ID=${!ACCESS_KEY_ID}
SECRET_ACCESS_KEY="${deploy_env}_AWS_SECRET_ACCESS_KEY"
echo export AWS_SECRET_ACCESS_KEY=${!SECRET_ACCESS_KEY}
DEFAULT_REGION="${deploy_env}_AWS_DEFAULT_REGION"
echo export AWS_DEFAULT_REGION=${!DEFAULT_REGION}
DOCKERREGISTRY="${deploy_env}_DOCKER_REGISTRY"
echo export DOCKER_REGISTRY=${!DOCKERREGISTRY}
tag="${deploy_env}_TAG"
echo export TAG=${!tag}
}

check(){
if [[ $# -lt 2 ]]; then
    echo "Usage: $( basename $0 ) <repository-name> <image-tag>"
    exit 1
fi
IMAGE_META="$( aws ecr describe-images --repository-name=$1 --image-ids=imageTag=$2 2> /dev/null )"
if [[ $? == 0 ]]; then
    IMAGE_TAGS="$( echo ${IMAGE_META} | jq '.imageDetails[0].imageTags[0]' -r )"
    echo "$1:$2 found"
else
    echo "$1:$2 not found"
    exit 1
fi
}

case "$1" in
    "") ;;
    variables) "$@"; exit;;
    check) "$@"; exit;;
esac

if [ -z "$TAGS" ];  
then  
echo "TAG not exist in repo" 
git tag -a $VERSION -m "repo is tagged"
git push origin $VERSION
git tag -d $VERSION
else  
echo "TAG exist in repo $TAGS" 
VERSION=$(increment)
echo "$VERSION" > version.txt
git add version.txt
git commit -m "updated version.txt"
git pull
git push --force origin HEAD:$CI_COMMIT_REF_NAME
git tag -a $VERSION -m "repo is tagged"
git push origin $VERSION
git tag -d $VERSION
fi  
